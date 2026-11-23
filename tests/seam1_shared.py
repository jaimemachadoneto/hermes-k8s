"""The Seam 1 suite: create the environment through the factory of Hermes, run commands.

The tests use only the port of the cluster, so the same functions run against the fake
cluster (`test_environment.py`) and against the real one (`live/test_live_seam1.py`).
"""


def test_a_command_returns_its_output_and_exit_code(make_env):
    env = make_env()

    ok = env.execute("echo hello")
    failed = env.execute("echo oops >&2; exit 3")

    assert ok["output"].strip() == "hello"
    assert ok["returncode"] == 0
    assert "oops" in failed["output"]
    assert failed["returncode"] == 3


def test_the_directory_and_the_exports_stay_between_commands(make_env):
    env = make_env()

    env.execute("mkdir -p project/src && cd project/src && export STAGE=build")
    pwd = env.execute("pwd")
    stage = env.execute('echo "stage=$STAGE"')

    assert pwd["output"].strip().endswith("/project/src")
    assert stage["output"].strip() == "stage=build"


def test_a_command_that_runs_too_long_ends_with_exit_code_124(make_env):
    env = make_env()

    result = env.execute("echo start; sleep 30", timeout=1)

    assert result["returncode"] == 124
    assert "start" in result["output"]
    # The shell of the sandbox is still usable after the kill.
    assert env.execute("echo alive")["output"].strip() == "alive"


def test_an_interrupt_stops_a_running_command_with_exit_code_130(make_env):
    import threading
    import time

    from tools.interrupt import set_interrupt

    env = make_env()
    tid = threading.current_thread().ident
    timer = threading.Timer(0.5, set_interrupt, args=(True, tid))
    timer.start()
    started = time.monotonic()
    try:
        result = env.execute("sleep 30", timeout=60)
    finally:
        timer.cancel()
        set_interrupt(False, tid)

    assert result["returncode"] == 130
    assert time.monotonic() - started < 10


# Shell text that runs a parent and a child, saves their PIDs, and waits.
_PARENT_AND_CHILD = "sleep 300 & echo $! > child.pid; echo $$ > parent.pid; wait"
_ALIVE = (
    'for f in parent.pid child.pid; do p=$(cat "$f"); [ -n "$p" ] || continue; '
    '[ -d "/proc/$p" ] && ! grep -q "^State:.Z" "/proc/$p/status" && echo "$f alive"; done; true'
)


def _alive(env) -> str:
    """Name the saved PIDs whose process still runs in the sandbox."""
    return env.execute(_ALIVE)["output"]


def _wait_until_gone(env) -> str:
    import time

    deadline = time.monotonic() + 15
    while True:
        alive = _alive(env)
        if not alive.strip() or time.monotonic() >= deadline:
            return alive
        time.sleep(0.2)


def _stop_leftovers(env) -> None:
    env.execute("kill -KILL $(cat parent.pid child.pid) 2>/dev/null; true")


def test_a_command_that_times_out_is_stopped_in_the_sandbox_with_its_children(make_env):
    env = make_env()
    try:
        result = env.execute(_PARENT_AND_CHILD, timeout=1)

        assert result["returncode"] == 124
        assert _wait_until_gone(env).strip() == ""
    finally:
        _stop_leftovers(env)


def test_a_command_that_the_user_cancels_is_stopped_in_the_sandbox_with_its_children(make_env):
    import threading

    from tools.interrupt import set_interrupt

    env = make_env()
    tid = threading.current_thread().ident
    timer = threading.Timer(1.0, set_interrupt, args=(True, tid))
    timer.start()
    try:
        result = env.execute(_PARENT_AND_CHILD, timeout=60)
        assert result["returncode"] == 130
    finally:
        timer.cancel()
        set_interrupt(False, tid)
    try:
        assert _wait_until_gone(env).strip() == ""
    finally:
        _stop_leftovers(env)


def test_a_command_that_ignores_the_first_signal_is_still_stopped(make_env):
    env = make_env()
    command = (
        "trap '' TERM; (trap '' TERM; sleep 300) & echo $! > child.pid; echo $$ > parent.pid; wait"
    )
    try:
        result = env.execute(command, timeout=1)

        assert result["returncode"] == 124
        assert _wait_until_gone(env).strip() == ""
    finally:
        _stop_leftovers(env)


def test_a_background_job_of_a_command_that_ended_is_not_stopped(make_env):
    env = make_env()
    try:
        result = env.execute("sleep 300 & echo $! > child.pid; echo $$ > parent.pid", timeout=30)
        assert result["returncode"] == 0

        # A later command and a later timeout do not touch the job of an earlier command.
        env.execute("sleep 30", timeout=1)

        assert "child.pid alive" in _alive(env)
    finally:
        _stop_leftovers(env)


def test_a_command_that_ends_keeps_its_exit_code_and_removes_its_directory(make_env):
    env = make_env()

    result = env.execute('grep -l "^$$\\$" /tmp/hermes-k8s/*/pgid > seen.txt; exit 7')

    assert result["returncode"] == 7
    seen = env.execute("cat seen.txt")["output"].split()
    assert len(seen) == 1
    assert env.execute(f"test -e {seen[0]} || echo gone")["output"].strip() == "gone"


def test_the_stop_keeps_the_directory_and_the_exports_of_later_commands(make_env):
    env = make_env()
    env.execute("mkdir -p kept && cd kept && export STAGE=one")

    env.execute("sleep 30", timeout=1)

    assert env.execute("pwd")["output"].strip().endswith("/kept")
    assert env.execute("echo $STAGE")["output"].strip() == "one"


def test_cleanup_suspends_the_sandbox_and_the_next_environment_of_the_task_resumes_it(
    make_env, cluster
):
    first = make_env("task-a")
    first.execute("echo kept > note.txt")
    first.cleanup()

    assert cluster.get_sandbox(first.sandbox_name).operating_mode == "Suspended"

    second = make_env("task-a")
    assert second.execute("cat note.txt")["output"].strip() == "kept"
    assert cluster.get_sandbox(first.sandbox_name).operating_mode == "Running"


def test_a_sandbox_that_nobody_suspended_is_attached_again_by_the_next_environment(
    make_env, cluster
):
    # A Hermes that is killed (SIGKILL, OOM) never calls cleanup. The next process attaches by name.
    first = make_env("task-crash")
    first.execute("echo kept > note.txt")

    second = make_env("task-crash")

    assert second.sandbox_name == first.sandbox_name
    assert second.execute("cat note.txt")["output"].strip() == "kept"
    assert [s.name for s in cluster.list_sandboxes()].count(first.sandbox_name) == 1


def test_each_task_id_has_its_own_sandbox_and_the_same_id_has_the_same_one(make_env):
    a = make_env("session:a")
    b = make_env("session:b")
    a_again = make_env("session:a")

    a.execute("echo from-a > marker")

    assert b.execute("cat marker 2>&1; true")["output"].strip() != "from-a"
    assert a_again.execute("cat marker")["output"].strip() == "from-a"
    assert a.sandbox_name == a_again.sandbox_name != b.sandbox_name


def test_the_environment_does_not_sync_files_with_the_hermes_home(make_env, tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / ".env").write_text("SECRET=1\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    env = make_env()
    env.execute("echo written-in-sandbox > from-sandbox.txt")
    absent = env.execute("test ! -e ~/.hermes && test ! -e ~/.env && echo absent")["output"]

    assert absent.strip() == "absent"
    assert sorted(p.name for p in home.iterdir()) == [".env"]


def test_standard_input_goes_to_the_command(make_env):
    env = make_env()

    result = env.execute("cat", stdin_data="line one\nline two")

    assert result["output"].strip() == "line one\nline two"


def test_a_given_working_directory_is_the_start_of_the_commands(make_env):
    env = make_env(cwd="/tmp")

    assert env.execute("pwd")["output"].strip() == "/tmp"


def test_a_command_cannot_escape_its_quoting(make_env):
    env = make_env()
    hostile = 'it\'s "quoted" $(echo injected) `echo injected` ; \\ \n done'

    result = env.execute("printf %s " + _single_quote(hostile))

    assert result["output"] == hostile


def _single_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"

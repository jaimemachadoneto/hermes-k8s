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

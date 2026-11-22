"""Seam 1: create the environment as Hermes does, run commands, clean up."""


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
    threading.Timer(0.5, set_interrupt, args=(True, tid)).start()
    started = time.monotonic()
    try:
        result = env.execute("sleep 30", timeout=60)
    finally:
        set_interrupt(False, tid)

    assert result["returncode"] == 130
    assert time.monotonic() - started < 10


def test_cleanup_suspends_the_sandbox_and_the_next_environment_of_the_task_resumes_it(
    make_env, cluster
):
    first = make_env("task-a")
    first.execute("echo kept > note.txt")
    first.cleanup()

    (suspended,) = cluster.sandboxes.values()
    assert suspended.operating_mode == "Suspended"

    second = make_env("task-a")
    assert second.execute("cat note.txt")["output"].strip() == "kept"
    assert len(cluster.sandboxes) == 1
    assert suspended.operating_mode == "Running"


def test_each_task_id_has_its_own_sandbox_and_the_same_id_has_the_same_one(make_env, cluster):
    a = make_env("session:a")
    b = make_env("session:b")
    a_again = make_env("session:a")

    a.execute("echo from-a > marker")

    assert len(cluster.sandboxes) == 2
    assert b.execute("ls")["output"].strip() == ""
    assert a_again.execute("cat marker")["output"].strip() == "from-a"
    assert a.sandbox_name == a_again.sandbox_name != b.sandbox_name


def test_the_sandbox_name_is_a_valid_name_of_at_most_63_characters(make_env, cluster):
    import re

    make_env("x" * 500)
    make_env("")
    make_env("Session With Spaces / and symbols é")

    names = list(cluster.sandboxes)
    assert len(names) == 3
    for name in names:
        assert len(name) <= 63
        assert re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name)


def test_the_environment_does_not_sync_files_with_the_hermes_home(
    make_env, cluster, tmp_path, monkeypatch
):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / ".env").write_text("SECRET=1\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    env = make_env()
    env.execute("echo written-in-sandbox > from-sandbox.txt")
    absent = env.execute("test ! -e ~/.hermes && test ! -e ~/.env && echo absent")["output"]
    env.cleanup()

    assert absent.strip() == "absent"
    assert not hasattr(env, "_sync_manager")
    assert sorted(p.name for p in home.iterdir()) == [".env"]


def test_standard_input_goes_to_the_command(make_env):
    env = make_env()

    result = env.execute("cat", stdin_data="line one\nline two")

    assert result["output"].strip() == "line one\nline two"

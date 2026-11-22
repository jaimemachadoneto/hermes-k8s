"""Suspend and resume: the idle clock, the sweep after a restart, and the calls of Hermes.

The fake cluster and the test clock stand for the cluster and for time. The tests that call
`_cleanup_inactive_envs`, `cleanup_vm` and `cleanup_all_environments` run the real code of Hermes.
"""

import time

import pytest
from images import IMAGE
from k8s_sandbox.cluster import RUNNING, SUSPENDED
from k8s_sandbox.lifecycle import IDLE_SECONDS, suspend_idle
from k8s_sandbox.provider import K8sSandboxProvider
from tools import terminal_tool
from tools.process_registry import process_registry
from tools.terminal_tool_lifecycle import (
    _cleanup_inactive_envs,
    cleanup_all_environments,
    cleanup_vm,
    is_persistent_env,
)


def mode(cluster, env):
    return cluster.get_sandbox(env.sandbox_name).operating_mode


@pytest.fixture
def hermes_cache():
    """Register the environment of a task in the cache that the idle reaper of Hermes reads."""
    keys = []

    def register(key, env):
        with terminal_tool._env_lock:
            terminal_tool._active_environments[key] = env
            terminal_tool._last_activity[key] = time.time()
        keys.append(key)

    yield register
    with terminal_tool._env_lock:
        for key in keys:
            terminal_tool._active_environments.pop(key, None)
            terminal_tool._last_activity.pop(key, None)


def make_stale(key, seconds=IDLE_SECONDS + 1):
    with terminal_tool._env_lock:
        terminal_tool._last_activity[key] = time.time() - seconds


# --- the idle clock of the plugin: the sweep that runs after a restart --------------------------


def test_the_sweep_suspends_a_sandbox_that_is_idle_for_more_than_the_limit(
    make_env, cluster, clock
):
    env = make_env("a")
    env.execute("echo hi")

    clock.advance(IDLE_SECONDS - 1)
    assert suspend_idle(cluster, now=clock(), idle_seconds=IDLE_SECONDS) == []
    assert mode(cluster, env) == RUNNING

    clock.advance(2)
    assert suspend_idle(cluster, now=clock(), idle_seconds=IDLE_SECONDS) == [env.sandbox_name]
    assert mode(cluster, env) == SUSPENDED


def test_a_command_restarts_the_idle_clock(make_env, cluster, clock):
    env = make_env("a")
    clock.advance(IDLE_SECONDS - 100)

    env.execute("echo still here")
    clock.advance(IDLE_SECONDS - 100)

    assert suspend_idle(cluster, now=clock(), idle_seconds=IDLE_SECONDS) == []
    assert mode(cluster, env) == RUNNING


def test_the_sweep_does_not_touch_a_suspended_sandbox_and_keeps_its_last_use(
    make_env, cluster, clock
):
    env = make_env("a")
    env.cleanup()
    suspended_at = cluster.get_sandbox(env.sandbox_name).last_used

    clock.advance(10 * IDLE_SECONDS)

    assert suspend_idle(cluster, now=clock(), idle_seconds=IDLE_SECONDS) == []
    assert (
        cluster.get_sandbox(env.sandbox_name).last_used
        == suspended_at
        == clock() - 10 * IDLE_SECONDS
    )


def test_the_sweep_starts_the_clock_of_a_sandbox_that_has_no_last_use(cluster, clock):
    # A sandbox from before this change has no last-use time. The sweep must not suspend it at once.
    cluster.create_sandbox("old", image=IMAGE, labels={})

    assert suspend_idle(cluster, now=clock(), idle_seconds=IDLE_SECONDS) == []
    assert cluster.get_sandbox("old").last_used == clock()
    clock.advance(IDLE_SECONDS + 1)
    assert suspend_idle(cluster, now=clock(), idle_seconds=IDLE_SECONDS) == ["old"]


# --- cleanup: suspend, never delete --------------------------------------------------------------


def test_cleanup_records_the_time_of_the_suspend(make_env, cluster, clock):
    env = make_env("a")
    clock.advance(500)

    env.cleanup()

    status = cluster.get_sandbox(env.sandbox_name)
    assert status.suspended and status.last_used == clock()


def test_the_next_environment_resumes_the_same_volume(make_env, cluster):
    first = make_env("a")
    first.execute("echo kept > /tmp/x; mkdir -p d && echo deep > d/f")
    volume = cluster.sandboxes[first.sandbox_name].pvc_name
    first.cleanup()

    second = make_env("a")

    assert cluster.sandboxes[second.sandbox_name].pvc_name == volume
    assert second.sandbox_name == first.sandbox_name
    assert second.execute("cat d/f")["output"].strip() == "deep"


# --- the calls of Hermes -------------------------------------------------------------------------


def test_hermes_treats_the_environment_as_persistent_so_it_skips_the_cleanup_after_a_turn(
    make_env, cluster, hermes_cache
):
    # The agent loop calls cleanup_vm after each turn, unless the environment is persistent.
    env = make_env("session:a")
    hermes_cache("session:a", env)

    assert is_persistent_env("session:a") is True


def test_the_idle_reaper_of_hermes_suspends_a_sandbox_after_the_lifetime(
    make_env, cluster, hermes_cache
):
    env = make_env("session:a")
    hermes_cache("session:a", env)

    _cleanup_inactive_envs(IDLE_SECONDS)
    assert mode(cluster, env) == RUNNING

    make_stale("session:a")
    _cleanup_inactive_envs(IDLE_SECONDS)
    assert mode(cluster, env) == SUSPENDED
    assert env.sandbox_name in cluster.sandboxes


def test_a_background_process_holds_the_sandbox_awake(make_env, cluster, hermes_cache):
    env = make_env("session:bg")
    hermes_cache("session:bg", env)
    session = process_registry.spawn_via_env(
        env, "sleep 60", task_id="session:bg", owner_task_id="session:bg", timeout=20
    )
    try:
        make_stale("session:bg")
        _cleanup_inactive_envs(IDLE_SECONDS)
        assert mode(cluster, env) == RUNNING
    finally:
        process_registry.kill_process(session.id)

    deadline = time.monotonic() + 10
    while (
        process_registry._any_running(lambda s: s.id == session.id) and time.monotonic() < deadline
    ):
        time.sleep(0.1)
    make_stale("session:bg")
    _cleanup_inactive_envs(IDLE_SECONDS)
    assert mode(cluster, env) == SUSPENDED


def test_the_shutdown_of_hermes_suspends_the_sandboxes_and_deletes_none(
    make_env, cluster, hermes_cache
):
    a, b = make_env("session:a"), make_env("session:b")
    a.execute("echo kept > note")
    hermes_cache("session:a", a)
    hermes_cache("session:b", b)

    cleanup_all_environments()  # the atexit hook of Hermes calls this

    assert set(cluster.sandboxes) == {a.sandbox_name, b.sandbox_name}
    assert mode(cluster, a) == mode(cluster, b) == SUSPENDED


def test_the_end_of_a_session_suspends_and_never_deletes(make_env, cluster, hermes_cache):
    env = make_env("session:a")
    hermes_cache("session:a", env)

    cleanup_vm("session:a")

    assert mode(cluster, env) == SUSPENDED
    assert env.sandbox_name in cluster.sandboxes


# --- attach after a restart ----------------------------------------------------------------------


def test_a_new_provider_attaches_to_the_sandbox_of_the_task(cluster, clock):
    first = K8sSandboxProvider(cluster_factory=lambda: cluster, image=IMAGE, clock=clock)
    env = first.create_environment(cwd="~", timeout=30, task_id="session:a")
    env.execute("echo kept > note.txt")
    env.cleanup()

    # A new process: a new provider with no memory of the first.
    second = K8sSandboxProvider(cluster_factory=lambda: cluster, image=IMAGE, clock=clock)
    again = second.create_environment(cwd="~", timeout=30, task_id="session:a")

    assert again.sandbox_name == env.sandbox_name
    assert len(cluster.sandboxes) == 1
    assert again.execute("cat note.txt")["output"].strip() == "kept"
    assert mode(cluster, again) == RUNNING


def test_a_failed_mark_does_not_fail_a_command(make_env, cluster):
    from k8s_sandbox.cluster import ClusterError

    env = make_env("a")

    def fail(name, at):
        raise ClusterError("boom")

    cluster.mark_used = fail

    assert env.execute("echo hi")["output"].strip() == "hi"


def test_commands_within_the_interval_write_the_mark_once(make_env, cluster, clock):
    calls = []
    real = cluster.mark_used
    cluster.mark_used = lambda name, at: (calls.append(at), real(name, at))[1]
    env = make_env("a")
    calls.clear()

    for _ in range(5):
        env.execute("true")
    assert calls == []  # the open of the sandbox marked it at this time already

    clock.advance(61)
    env.execute("true")
    assert len(calls) >= 1


def test_the_sweep_goes_on_after_one_sandbox_fails(cluster, clock):
    from k8s_sandbox.cluster import ClusterError

    for name in ("a", "b"):
        cluster.create_sandbox(name, image=IMAGE, labels={})
        cluster.mark_used(name, clock())
    clock.advance(IDLE_SECONDS + 1)
    real = cluster.set_operating_mode

    def flaky(name, mode):
        if name == "a":
            raise ClusterError("boom")
        real(name, mode)

    cluster.set_operating_mode = flaky

    assert suspend_idle(cluster, now=clock()) == ["b"]


def test_the_sweep_skips_a_sandbox_that_a_command_used_after_the_list(cluster, clock):
    cluster.create_sandbox("a", image=IMAGE, labels={})
    cluster.mark_used("a", clock())
    clock.advance(IDLE_SECONDS + 1)
    real = cluster.list_sandboxes

    def list_then_use():
        statuses = real()
        cluster.mark_used("a", clock())  # a command runs between the list and the suspend
        return statuses

    cluster.list_sandboxes = list_then_use

    assert suspend_idle(cluster, now=clock()) == []
    assert cluster.get_sandbox("a").operating_mode == RUNNING


def test_the_sweep_at_start_logs_a_failure_and_does_not_raise(monkeypatch, caplog):
    import k8s_sandbox

    def broken():
        raise RuntimeError("no cluster")

    monkeypatch.setattr(k8s_sandbox, "_cluster", broken)

    k8s_sandbox._suspend_left_running()

    assert "Could not suspend the idle sandboxes" in caplog.text

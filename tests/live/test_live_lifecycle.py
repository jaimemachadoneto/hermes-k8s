"""Suspend and resume in the real cluster: the home, the volume, the idle sweep, no delete."""

import time

import pytest
from k8s_sandbox.cluster import RUNNING, SUSPENDED, sandbox_name
from k8s_sandbox.lifecycle import IDLE_SECONDS, suspend_idle

pytestmark = pytest.mark.live


def mine(run_id):
    """Sweep only the sandboxes of this test run, never the sandboxes of a real session."""

    def accept(name):
        return any(name == sandbox_name(f"live-{run_id}-{t}") for t in TASKS)

    return accept


TASKS = ("idle", "busy")


def test_the_home_is_in_the_workspace_and_the_tmp_is_empty_after_a_resume(make_env, cluster):
    first = make_env("home")
    assert first.execute("echo $HOME")["output"].strip() == "/workspace/.home"
    first.execute("echo kept > ~/.dotfile; echo gone > /tmp/scratch")
    first.cleanup()
    assert cluster.get_sandbox(first.sandbox_name).suspended

    second = make_env("home")

    assert second.execute("cat ~/.dotfile")["output"].strip() == "kept"
    # /tmp is an emptyDir: a new pod gets a new one.
    assert second.execute("test ! -e /tmp/scratch && echo empty")["output"].strip() == "empty"


def test_the_idle_sweep_suspends_a_sandbox_that_was_not_used_for_30_minutes(
    make_env, cluster, run_id
):
    env = make_env("idle")
    env.execute("echo kept > /workspace/note")
    cluster.mark_used(env.sandbox_name, time.time() - IDLE_SECONDS - 60)

    suspended = suspend_idle(cluster, now=time.time(), only=mine(run_id))

    assert env.sandbox_name in suspended
    assert cluster.get_sandbox(env.sandbox_name).operating_mode == SUSPENDED
    # The next environment of the task resumes it on the same volume.
    assert make_env("idle").execute("cat /workspace/note")["output"].strip() == "kept"
    assert cluster.get_sandbox(env.sandbox_name).operating_mode == RUNNING


def test_the_sweep_leaves_a_sandbox_that_a_command_used_a_moment_ago(make_env, cluster, run_id):
    env = make_env("busy")
    env.execute("true")

    assert env.sandbox_name not in suspend_idle(cluster, now=time.time(), only=mine(run_id))
    assert cluster.get_sandbox(env.sandbox_name).operating_mode == RUNNING


def test_a_cleanup_does_not_delete_the_sandbox(make_env, cluster):
    env = make_env("keep")
    env.cleanup()

    status = cluster.get_sandbox(env.sandbox_name)
    assert status is not None and status.suspended
    assert status.last_used is not None


def test_a_suspend_stops_a_process_that_a_timed_out_command_left(make_env):
    # The exec closes at the timeout, and the process stays until the pod stops (ticket 08).
    env = make_env("orphan")
    env.execute(
        "rm -f /workspace/tick; (while :; do date +%s > /workspace/tick; sleep 1; done) & sleep 30",
        timeout=3,
    )
    env.cleanup()

    again = make_env("orphan")
    stamp = again.execute("cat /workspace/tick")["output"].strip()
    time.sleep(3)

    assert again.execute("cat /workspace/tick")["output"].strip() == stamp

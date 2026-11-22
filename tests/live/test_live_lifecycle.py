"""Suspend and resume in the real cluster: the home, the volume, the idle sweep, no delete."""

import time

import pytest
from k8s_sandbox.cluster import RUNNING, SUSPENDED
from k8s_sandbox.lifecycle import IDLE_SECONDS, suspend_idle

pytestmark = pytest.mark.live


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


def test_the_idle_sweep_suspends_a_sandbox_that_was_not_used_for_30_minutes(make_env, cluster):
    env = make_env("idle")
    env.execute("echo kept > /workspace/note")
    cluster.mark_used(env.sandbox_name, time.time() - IDLE_SECONDS - 60)

    suspended = suspend_idle(cluster, now=time.time())

    assert env.sandbox_name in suspended
    assert cluster.get_sandbox(env.sandbox_name).operating_mode == SUSPENDED
    # The next environment of the task resumes it on the same volume.
    assert make_env("idle").execute("cat /workspace/note")["output"].strip() == "kept"
    assert cluster.get_sandbox(env.sandbox_name).operating_mode == RUNNING


def test_the_sweep_leaves_a_sandbox_that_a_command_used_a_moment_ago(make_env, cluster):
    env = make_env("busy")
    env.execute("true")

    assert env.sandbox_name not in suspend_idle(cluster, now=time.time())
    assert cluster.get_sandbox(env.sandbox_name).operating_mode == RUNNING


def test_a_cleanup_does_not_delete_the_sandbox(make_env, cluster):
    env = make_env("keep")
    env.cleanup()

    status = cluster.get_sandbox(env.sandbox_name)
    assert status is not None and status.suspended
    assert status.last_used is not None


def test_a_command_that_timed_out_runs_on_until_the_suspend(make_env):
    # Open item of ticket 08: the exec closes, the process stays. Suspend stops it with the pod.
    env = make_env("orphan")
    env.execute(
        "rm -f /workspace/tick; (while :; do date +%s > /workspace/tick; sleep 1; done) & sleep 30",
        timeout=3,
    )
    first = env.execute("cat /workspace/tick")["output"].strip()
    time.sleep(3)
    second = env.execute("cat /workspace/tick")["output"].strip()

    assert first and second and second != first  # the process is alive after the timeout
    env.cleanup()
    again = make_env("orphan")
    a = again.execute("cat /workspace/tick")["output"].strip()
    time.sleep(3)
    assert again.execute("cat /workspace/tick")["output"].strip() == a  # the suspend stopped it

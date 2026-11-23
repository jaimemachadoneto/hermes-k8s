"""Seam 1 through the fake cluster: the shared suite, and the tests that need its internals."""

from images import IMAGE
from seam1_shared import *


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


def test_two_long_ids_with_one_prefix_have_two_sandboxes(make_env, cluster):
    make_env("x" * 500)
    make_env("x" * 500 + "y")

    assert len(cluster.sandboxes) == 2


def test_a_command_reports_the_error_when_the_sandbox_is_gone(make_env, cluster):
    env = make_env()
    cluster.delete_sandbox(env.sandbox_name)

    result = env.execute("echo hi")

    assert result["returncode"] == 1
    assert "sandbox error" in result["output"]


def test_cleanup_does_not_raise_when_the_sandbox_cannot_be_suspended(make_env, cluster):
    env = make_env()
    cluster.delete_sandbox(env.sandbox_name)

    env.cleanup()


def test_the_environment_fails_when_the_pod_never_becomes_ready(registered, cluster):
    import pytest
    from k8s_sandbox.cluster import SandboxNotReady

    cluster.never_ready = True

    with pytest.raises(SandboxNotReady):
        registered.create_environment(cwd="/tmp", timeout=5, task_id="slow", ready_timeout=0.1)


def test_the_provider_refuses_to_create_an_environment_without_a_cluster_client():
    import pytest
    from k8s_sandbox.provider import K8sSandboxProvider

    with pytest.raises(RuntimeError):
        K8sSandboxProvider(image=IMAGE).create_environment(cwd="/tmp", timeout=5)


def test_the_provider_refuses_to_create_an_environment_without_an_image(cluster):
    import pytest
    from k8s_sandbox.provider import K8sSandboxProvider

    with pytest.raises(ValueError):
        K8sSandboxProvider(cluster_factory=lambda: cluster).create_environment(
            cwd="/tmp", timeout=5
        )


def test_the_provider_refuses_an_image_without_a_digest(cluster):
    import pytest
    from k8s_sandbox.provider import K8sSandboxProvider

    provider = K8sSandboxProvider(cluster_factory=lambda: cluster)
    for image in (
        "ghcr.io/jaimemachadoneto/hermes-sandbox",
        "ghcr.io/jaimemachadoneto/hermes-sandbox:y2iwr4mjg4pn",
        "ghcr.io/jaimemachadoneto/hermes-sandbox@sha256:abc",
    ):
        with pytest.raises(ValueError, match="digest"):
            provider.create_environment(cwd="/tmp", timeout=5, image=image)
    assert cluster.sandboxes == {}


def test_the_configured_image_wins_over_the_image_that_hermes_passes(cluster):
    from k8s_sandbox.provider import K8sSandboxProvider

    provider = K8sSandboxProvider(cluster_factory=lambda: cluster, image=IMAGE)

    # Hermes passes its own container image setting. That image is not a sandbox image.
    provider.create_environment(cwd="/tmp", timeout=5, image="python:3.12")

    (sandbox,) = cluster.sandboxes.values()
    assert sandbox.image == IMAGE


def test_a_command_that_ends_normally_sends_no_stop_exec(make_env, cluster):
    env = make_env()
    cluster.exec_log.clear()

    env.execute("true")
    env.execute("exit 3")

    assert len(cluster.exec_log) == 2


def test_a_timeout_sends_one_stop_exec(make_env, cluster):
    env = make_env()
    cluster.exec_log.clear()

    env.execute("sleep 30", timeout=1)

    assert len(cluster.exec_log) == 2


def test_a_command_that_exits_124_by_itself_keeps_its_exit_code_and_kills_nothing(make_env):
    env = make_env()
    env.execute("sleep 300 & echo $! > child.pid", timeout=30)
    try:
        result = env.execute("exit 124")

        assert result["returncode"] == 124
        alive = env.execute('p=$(cat child.pid); [ -d "/proc/$p" ] && echo alive; true')
        assert alive["output"].strip() == "alive"
    finally:
        env.execute("kill $(cat child.pid) 2>/dev/null; true")


def test_a_failed_stop_exec_keeps_the_timeout_exit_code(make_env, cluster):
    from k8s_sandbox.cluster import ClusterError

    env = make_env()
    real = cluster.exec

    def exec_then_fail(name, argv, **kwargs):
        if "k8s-stop" in argv:
            raise ClusterError("boom")
        return real(name, argv, **kwargs)

    cluster.exec = exec_then_fail
    try:
        result = env.execute("echo start; sleep 30", timeout=1)
    finally:
        cluster.exec = real

    assert result["returncode"] == 124
    assert "start" in result["output"]

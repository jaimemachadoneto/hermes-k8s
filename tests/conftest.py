# pyright: reportAttributeAccessIssue=false
# The registry of Hermes exports its functions with a call at import time.
"""Fixtures that create the environment the way Hermes does (Seam 1)."""

import pytest
from agent import terminal_env_registry
from k8s_sandbox.fake_cluster import FakeCluster
from k8s_sandbox.provider import K8sSandboxProvider
from tools.terminal_tool_backends import _create_environment


@pytest.fixture
def cluster(tmp_path):
    return FakeCluster(root=tmp_path / "cluster")


@pytest.fixture
def registered(cluster):
    """The backend registered in Hermes, bound to the fake cluster."""
    terminal_env_registry._reset_for_tests()
    provider = K8sSandboxProvider(cluster_factory=lambda: cluster, image="sandbox:test")
    terminal_env_registry.register_provider(provider)
    yield provider
    terminal_env_registry._reset_for_tests()


@pytest.fixture
def make_env(registered):
    """Call the factory of Hermes for the backend. Clean up each environment at the end."""
    made = []

    def make(task_id="task-1", timeout=30):
        env = _create_environment(
            env_type=registered.name,
            image="",
            cwd=registered.default_cwd,
            timeout=timeout,
            task_id=task_id,
            container_config={},
        )
        made.append(env)
        return env

    yield make
    for env in made:
        env.cleanup()

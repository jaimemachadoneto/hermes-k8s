# pyright: reportAttributeAccessIssue=false
# The registry of Hermes exports its functions with a call at import time.
"""The live suite: it runs against the namespace hermes-sandbox of the real cluster.

It runs only on request: `HERMES_K8S_LIVE=1` and a `KUBECONFIG` (see `smoke-live` in
`devenv.nix`). Without them `check` skips each test here. Each test deletes the sandboxes that it
makes.
"""

import os
import uuid

import pytest
from agent import terminal_env_registry
from k8s_sandbox.cluster import SandboxNotFound
from k8s_sandbox.kubernetes_cluster import KubernetesCluster
from k8s_sandbox.provider import K8sSandboxProvider
from tools.terminal_tool_backends import _create_environment

# The first pushed image of ticket 05. Set HERMES_K8S_IMAGE to test another one.
DEFAULT_IMAGE = (
    "ghcr.io/jaimemachadoneto/hermes-sandbox:y2iwr4mjg4pn"
    "@sha256:bc56a0949f68efd3ba28e63a83a9e1b00e73a4ea0b8fef9816b228e038912f73"
)


def pytest_collection_modifyitems(config, items):
    if os.environ.get("HERMES_K8S_LIVE") == "1" and os.environ.get("KUBECONFIG"):
        return
    skip = pytest.mark.skip(reason="live suite: set HERMES_K8S_LIVE=1 and KUBECONFIG")
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def run_id():
    return uuid.uuid4().hex[:8]


@pytest.fixture
def cluster():
    return KubernetesCluster.from_kubeconfig(os.environ["KUBECONFIG"])


@pytest.fixture
def allowed_services():
    """The service allowances of the profile in a test. A test adds a name before `make_env`."""
    return []


@pytest.fixture
def registered(cluster, allowed_services):
    terminal_env_registry._reset_for_tests()
    image = os.environ.get("HERMES_K8S_IMAGE", DEFAULT_IMAGE)
    provider = K8sSandboxProvider(
        cluster_factory=lambda: cluster, image=image, allowances=lambda: list(allowed_services)
    )
    terminal_env_registry.register_provider(provider)
    yield provider
    terminal_env_registry._reset_for_tests()


@pytest.fixture
def make_env(registered, cluster, run_id):
    """The factory of Hermes for the backend. Each sandbox that it makes is deleted at the end."""
    made = []

    def make(task_id="task-1", timeout=60, cwd=None):
        env = _create_environment(
            env_type=registered.name,
            image="",
            cwd=cwd or registered.default_cwd,
            timeout=timeout,
            task_id=f"live-{run_id}-{task_id}",
            container_config={},
        )
        made.append(env)
        return env

    yield make
    for env in made:
        env.cleanup()
    for name in {env.sandbox_name for env in made}:
        try:
            cluster.delete_sandbox(name)
        except SandboxNotFound:
            pass

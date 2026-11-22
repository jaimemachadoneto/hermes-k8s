"""Hermes terminal-backend plugin: one Kubernetes sandbox for each session."""

import functools
import logging
import os

from .kubernetes_cluster import KubernetesCluster
from .provider import K8sSandboxProvider

logger = logging.getLogger(__name__)


@functools.cache
def _cluster() -> KubernetesCluster:
    """Use the account of the Hermes pod, or the kubeconfig that KUBECONFIG names."""
    return KubernetesCluster.from_kubeconfig(os.environ.get("KUBECONFIG"))


def register(ctx) -> None:
    # The sandbox image is pinned by digest (the admission policy needs it). The deployment sets it.
    image = os.environ.get("HERMES_K8S_SANDBOX_IMAGE", "")
    if not image:
        logger.warning("HERMES_K8S_SANDBOX_IMAGE is not set: the backend cannot start a sandbox")
    ctx.register_terminal_environment_provider(K8sSandboxProvider(_cluster, image))

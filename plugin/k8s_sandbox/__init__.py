"""Hermes terminal-backend plugin: one Kubernetes sandbox for each session."""

import functools
import logging
import os

from .allowances import SETTING, allowance_labels, services_for_profile
from .kubernetes_cluster import KubernetesCluster
from .provider import K8sSandboxProvider

logger = logging.getLogger(__name__)

# The name that Hermes gives to the profile of the Hermes home that is not in `profiles/`.
DEFAULT_PROFILE = "default"


@functools.cache
def _cluster() -> KubernetesCluster:
    """Use the account of the Hermes pod, or the kubeconfig that KUBECONFIG names."""
    return KubernetesCluster.from_kubeconfig(os.environ.get("KUBECONFIG"))


def register(ctx) -> None:
    # The sandbox image is pinned by digest (the admission policy needs it). The deployment sets it.
    image = os.environ.get("HERMES_K8S_SANDBOX_IMAGE", "")
    if not image:
        logger.warning("HERMES_K8S_SANDBOX_IMAGE is not set: the backend cannot start a sandbox")

    def allowances() -> list[str]:
        # Read at each sandbox, because one Hermes process can serve more than one profile.
        from hermes_cli.profiles import current_profile_name

        profile = current_profile_name(DEFAULT_PROFILE) or DEFAULT_PROFILE
        services = services_for_profile(ctx.get_config(SETTING), profile)
        try:
            allowance_labels(services)
        except ValueError as error:
            # The operator must see which profile has the wrong entry.
            raise ValueError(f"{SETTING} of the profile {profile!r}: {error}") from error
        return services

    ctx.register_terminal_environment_provider(K8sSandboxProvider(_cluster, image, allowances))

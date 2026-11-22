"""Hermes terminal-backend plugin: one Kubernetes sandbox for each session."""

import functools
import logging
import os
import threading
import time

from .allowances import SETTING, allowance_labels, services_for_profile
from .kubernetes_cluster import KubernetesCluster
from .lifecycle import suspend_idle
from .provider import K8sSandboxProvider

logger = logging.getLogger(__name__)

# The name that Hermes gives to the profile of the Hermes home that is not in `profiles/`.
DEFAULT_PROFILE = "default"


@functools.cache
def _cluster() -> KubernetesCluster:
    """Use the account of the Hermes pod, or the kubeconfig that KUBECONFIG names."""
    return KubernetesCluster.from_kubeconfig(os.environ.get("KUBECONFIG"))


def _suspend_left_running() -> None:
    """Suspend the sandboxes that an earlier process left running (a kill, an OOM, a crash)."""
    try:
        names = suspend_idle(_cluster(), now=time.time())
    except Exception:
        logger.warning("Could not suspend the idle sandboxes at start", exc_info=True)
        return
    if names:
        logger.info("Suspended %d idle sandbox(es) at start: %s", len(names), ", ".join(names))


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
    # In a thread: the call goes to the cluster, and the start of Hermes must not wait for it.
    threading.Thread(target=_suspend_left_running, name="k8s-sandbox-sweep", daemon=True).start()

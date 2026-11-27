"""Hermes terminal-backend plugin: one Kubernetes sandbox for each session."""

import functools
import logging
import os
import threading
import time

from .allowances import SETTING, allowance_labels, services_for_profile
from .archive import REAP_INTERVAL_SECONDS, reap_expired
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


def _maintain(stop: threading.Event, interval: float = REAP_INTERVAL_SECONDS) -> None:
    """Run the reaper of the expired sandboxes now, and then each `interval` until `stop` is set.

    A failed run is logged. The next run comes at the next interval.
    """
    while True:
        try:
            result = reap_expired(_cluster(), now=time.time())
            if result.deleted or result.deleted_archives:
                logger.info(
                    "Archived and deleted %d expired sandbox(es), deleted %d old archive(s)",
                    len(result.deleted),
                    len(result.deleted_archives),
                )
        except Exception:
            logger.warning("Could not reap the expired sandboxes", exc_info=True)
        if stop.wait(interval):
            return


def _start_sweeps() -> None:
    _suspend_left_running()
    # Nothing sets this event: the thread runs while Hermes runs. The tests set it to stop.
    _maintain(threading.Event())


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
    # In a thread: the calls go to the cluster, and the start of Hermes must not wait for them.
    # The thread runs while Hermes runs, so the reaper of the expired sandboxes runs each hour.
    threading.Thread(target=_start_sweeps, name="k8s-sandbox-sweep", daemon=True).start()

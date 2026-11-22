"""The idle clock of the sandboxes: when a sandbox was last used, and when to suspend it.

Hermes suspends a sandbox in its own process: its idle reaper calls `cleanup` after
`terminal.lifetime_seconds` (1800 in the overlay), and it keeps a sandbox that has a background
process. That reaper has no memory of a process that died. The last-use time on the Sandbox gives
the plugin that memory, and the TTL of a suspended sandbox (ticket 14) reads the same time.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .cluster import RUNNING, SUSPENDED, Cluster, ClusterError

logger = logging.getLogger(__name__)

# The same value as `terminal.lifetime_seconds` in the overlay of home-ops.
IDLE_SECONDS = 1800
# One command does not write the annotation more than once for each interval.
MARK_INTERVAL_SECONDS = 60

Clock = Callable[[], float]


class UseMarker:
    """Write the last-use time of one sandbox, at most once for each interval."""

    def __init__(self, cluster: Cluster, name: str, clock: Clock):
        self._cluster = cluster
        self._name = name
        self._clock = clock
        self._last: float | None = None

    def mark(self, *, force: bool = False) -> None:
        now = self._clock()
        if not force and self._last is not None and now - self._last < MARK_INTERVAL_SECONDS:
            return
        try:
            self._cluster.mark_used(self._name, now)
        except ClusterError:
            # The idle clock is a safety net. A failed mark must not fail a command.
            logger.warning("Could not mark the use of sandbox %s", self._name, exc_info=True)
            return
        self._last = now


def suspend_idle(cluster: Cluster, *, now: float, idle_seconds: float = IDLE_SECONDS) -> list[str]:
    """Suspend each running sandbox that was idle for more than `idle_seconds`.

    Call it once, when the process starts: it finds the sandboxes that an earlier process left
    running. Do not call it in a loop. It cannot see the background processes that Hermes tracks,
    and the idle reaper of Hermes does that work while Hermes runs.
    A sandbox with no last-use time gets one now, so it is not suspended at once.
    Return the names of the sandboxes that it suspended.
    """
    suspended = []
    for status in cluster.list_sandboxes():
        if status.operating_mode == SUSPENDED:
            continue
        if status.last_used is None:
            cluster.mark_used(status.name, now)
        elif now - status.last_used > idle_seconds and status.operating_mode == RUNNING:
            cluster.set_operating_mode(status.name, SUSPENDED)
            suspended.append(status.name)
    return suspended

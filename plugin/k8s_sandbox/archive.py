"""The TTL of a suspended sandbox (ticket 14): a sandbox archive, then the delete.

A sandbox that stays suspended for `EXPIRE_SECONDS` after its last use expires. The reaper makes a
sandbox archive of its volume, waits until the archive is ready, and then deletes the sandbox. The
delete of a Sandbox deletes its volume, so the archive must be ready first. The reaper deletes each
sandbox archive after `ARCHIVE_KEEP_SECONDS`. The chat history stays in Hermes: an archive keeps
only the files of the workspace.

The reaper reads the last-use time that each command writes (see `lifecycle`). It does not set
`shutdownTime` on the Sandbox: the controller would then stop the pod on its own clock, and the
idle reaper of Hermes already does that.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .cluster import SUSPENDED, Cluster, ClusterError, SandboxStatus

logger = logging.getLogger(__name__)

DAY_SECONDS = 24 * 3600.0
EXPIRE_SECONDS = 7 * DAY_SECONDS
ARCHIVE_KEEP_SECONDS = 30 * DAY_SECONDS
# How often the plugin runs the reaper while Hermes runs.
REAP_INTERVAL_SECONDS = 3600.0
# A Ceph RBD snapshot is ready in seconds. A slow one is tried again at the next run.
ARCHIVE_TIMEOUT_SECONDS = 300.0


@dataclass
class ReapResult:
    # The sandboxes that the reaper deleted, each after its archive was ready.
    deleted: list[str] = field(default_factory=list)
    deleted_archives: list[str] = field(default_factory=list)


def _expired(status: SandboxStatus | None, now: float, expire_seconds: float) -> bool:
    # A sandbox with no last-use time gets one from the idle sweep at start, so it expires later.
    return (
        status is not None
        and status.operating_mode == SUSPENDED
        and status.last_used is not None
        and now - status.last_used > expire_seconds
    )


def reap_expired(
    cluster: Cluster,
    *,
    now: float,
    expire_seconds: float = EXPIRE_SECONDS,
    keep_seconds: float = ARCHIVE_KEEP_SECONDS,
    archive_timeout: float = ARCHIVE_TIMEOUT_SECONDS,
) -> ReapResult:
    """Archive and delete each expired sandbox. Delete each sandbox archive that expired.

    A running sandbox is never touched. A failure on one sandbox or archive does not stop the run.
    When the list of the archives fails, the reaper deletes no sandbox: it cannot know which
    archive exists, and a second archive of each sandbox would fill the quota.
    """
    result = ReapResult()
    try:
        archives = cluster.list_archives()
    except ClusterError as error:
        logger.warning("Cannot list the sandbox archives, so no sandbox expires now: %s", error)
        return result

    ready = {a.sandbox for a in archives if a.ready}
    for status in cluster.list_sandboxes():
        if not _expired(status, now, expire_seconds):
            continue
        name = status.name
        try:
            # Read again: a command can have resumed the sandbox since the list.
            if not _expired(cluster.get_sandbox(name), now, expire_seconds):
                continue
            if name not in ready:
                cluster.archive_sandbox(
                    name, expires_at=now + keep_seconds, timeout=archive_timeout
                )
            cluster.delete_sandbox(name)
            result.deleted.append(name)
        except ClusterError as error:
            logger.warning("Cannot archive and delete the expired sandbox %s: %s", name, error)

    for archive in archives:
        if archive.expires_at is None or archive.expires_at > now:
            continue
        try:
            cluster.delete_archive(archive.name)
            result.deleted_archives.append(archive.name)
        except ClusterError as error:
            logger.warning("Cannot delete the sandbox archive %s: %s", archive.name, error)
    return result

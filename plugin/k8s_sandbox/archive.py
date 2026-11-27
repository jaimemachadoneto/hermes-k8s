"""The TTL of a suspended sandbox (ticket 14, ADR 0013): a sandbox archive, then the delete.

A sandbox that stays suspended for `EXPIRE_SECONDS` after its last use expires. The reaper makes a
sandbox archive of its volume, waits until the archive is ready, and then deletes the sandbox. The
delete of a Sandbox deletes its volume, so the archive must be ready first. The reaper deletes each
sandbox archive after `ARCHIVE_KEEP_SECONDS`. The chat history stays in Hermes. An archive is a
copy of the whole volume of the sandbox.

Each last use also moves the `shutdownTime` of the Sandbox (`Cluster.mark_used`). The controller
then stops the pod of an expired sandbox also when Hermes does not run. Only the reaper deletes.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from .cluster import SANDBOX_TTL_SECONDS, SUSPENDED, Cluster, ClusterError, SandboxStatus

logger = logging.getLogger(__name__)

DAY_SECONDS = 24 * 3600.0
EXPIRE_SECONDS = SANDBOX_TTL_SECONDS
ARCHIVE_KEEP_SECONDS = 30 * DAY_SECONDS
# How often the plugin runs the reaper while Hermes runs.
REAP_INTERVAL_SECONDS = 3600.0
# A Ceph RBD snapshot is ready in seconds. The next run waits again for a slow one.
ARCHIVE_TIMEOUT_SECONDS = 300.0


@dataclass
class ReapResult:
    # The sandboxes that the reaper deleted, each after its archive was ready.
    deleted: list[str] = field(default_factory=list)
    deleted_archives: list[str] = field(default_factory=list)


def _expired(status: SandboxStatus | None, now: float, expire_seconds: float) -> bool:
    return (
        status is not None
        and status.managed
        and status.operating_mode == SUSPENDED
        and status.last_used is not None
        and now - status.last_used > expire_seconds
    )


def _reap_sandbox(
    cluster: Cluster,
    status: SandboxStatus,
    *,
    now: float,
    expire_seconds: float,
    keep_seconds: float,
    archive_timeout: float,
) -> bool:
    """Archive and delete one expired sandbox. Return True when it was deleted."""
    name = status.name
    # Read again: a command can have resumed the sandbox since the list.
    before = cluster.get_sandbox(name)
    if not _expired(before, now, expire_seconds) or before is None or before.last_used is None:
        return False
    cluster.archive_sandbox(
        name, last_used=before.last_used, expires_at=now + keep_seconds, timeout=archive_timeout
    )
    # Read again after the wait. The archive holds the volume of this last use only.
    after = cluster.get_sandbox(name)
    if not _expired(after, now, expire_seconds) or after is None:
        return False
    if after.last_used != before.last_used:
        return False
    # A command that resumes the sandbox after this read changes its version, and the API server
    # then refuses the delete.
    cluster.delete_sandbox(name, resource_version=after.resource_version)
    return True


def reap_expired(
    cluster: Cluster,
    *,
    now: float,
    expire_seconds: float = EXPIRE_SECONDS,
    keep_seconds: float = ARCHIVE_KEEP_SECONDS,
    archive_timeout: float = ARCHIVE_TIMEOUT_SECONDS,
) -> ReapResult:
    """Archive and delete each expired sandbox. Delete each sandbox archive that expired.

    The reaper acts only on a Sandbox that the plugin made, and never on a running one. A failure
    on one sandbox or archive does not stop the run. A suspended sandbox with no last-use time
    gets one now, so it expires `expire_seconds` later.
    """
    result = ReapResult()
    for status in cluster.list_sandboxes():
        if not status.managed or status.operating_mode != SUSPENDED:
            continue
        try:
            if status.last_used is None:
                cluster.mark_used(status.name, now)
                logger.info("Sandbox %s had no last-use time; it expires later", status.name)
                continue
            if not _expired(status, now, expire_seconds):
                continue
            if _reap_sandbox(
                cluster,
                status,
                now=now,
                expire_seconds=expire_seconds,
                keep_seconds=keep_seconds,
                archive_timeout=archive_timeout,
            ):
                result.deleted.append(status.name)
        except ClusterError as error:
            logger.warning(
                "Cannot archive and delete the expired sandbox %s: %s", status.name, error
            )

    try:
        archives = cluster.list_archives()
    except ClusterError as error:
        logger.warning("Cannot list the sandbox archives, so none is deleted now: %s", error)
        return result
    for archive in archives:
        # An archive with no valid expiry stays. A person examines it.
        expires_at = archive.expires_at
        if expires_at is None or not math.isfinite(expires_at) or expires_at > now:
            continue
        try:
            cluster.delete_archive(archive.name)
            result.deleted_archives.append(archive.name)
        except ClusterError as error:
            logger.warning("Cannot delete the sandbox archive %s: %s", archive.name, error)
    return result

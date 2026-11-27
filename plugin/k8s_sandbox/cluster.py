"""The cluster port: the small interface between the plugin and Kubernetes.

The environment of the plugin talks only to this port. The `FakeCluster` keeps the
objects in memory for tests. `KubernetesCluster` is the client of the real API behind it.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Protocol

from .sandbox_spec import DEFAULT_PROFILE

NAMESPACE = "hermes-sandbox"
RUNNING = "Running"
SUSPENDED = "Suspended"
# The annotation that holds the last-use time of a sandbox (seconds since the epoch). The idle
# sweep and the TTL of a suspended sandbox read it.
LAST_USED_ANNOTATION = "hermes-k8s/last-used"
# The annotations that tell a person which Hermes session and profile made a sandbox. The name of
# a sandbox is a hash, so without them a sandbox cannot be traced back. The value of the task
# annotation is the task id that Hermes gives the plugin.
TASK_ANNOTATION = "hermes-k8s/task"
PROFILE_ANNOTATION = "hermes-k8s/profile"
# The label that the plugin puts on each Sandbox that it makes. The reaper acts only on these.
TASK_LABEL = "hermes-task"
# A sandbox expires 7 days after its last use (ticket 14, ADR 0013). Each last use moves the
# `shutdownTime` of the Sandbox to this time after it.
SANDBOX_TTL_SECONDS = 7 * 24 * 3600.0

# A Kubernetes object name is at most 63 characters (a DNS label).
_NAME_PREFIX = "hermes-"
_NAME_HASH_LENGTH = 40


def sandbox_name(task_id: str, profile: str = DEFAULT_PROFILE) -> str:
    """Name the sandbox of a task from a hash of its id.

    The same id gives the same name. The hash keeps the name valid for any id. A profile that is
    not the default is part of the hash, so a sandbox never runs with the tokens of another
    profile. The name of the default profile stays as it was.
    """
    key = task_id if profile == DEFAULT_PROFILE else f"{task_id}\0{profile}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return f"{_NAME_PREFIX}{digest[:_NAME_HASH_LENGTH]}"


class ClusterError(RuntimeError):
    """The cluster refuses or fails an operation."""


class SandboxNotFound(ClusterError):
    """The sandbox does not exist."""


class SandboxNotReady(ClusterError):
    """The sandbox does not become ready before the deadline."""


class QuotaExceeded(ClusterError):
    """The ResourceQuota of the namespace refuses the object. The message names the quota."""


@dataclass(frozen=True)
class SandboxStatus:
    """What the plugin reads from a Sandbox: its mode and its conditions."""

    name: str
    operating_mode: str
    ready: bool
    suspended: bool
    last_used: float | None = None
    # The resourceVersion of the object. A delete with it fails when the object changed.
    resource_version: str | None = None
    # True when the plugin made the Sandbox (it has the label TASK_LABEL).
    managed: bool = True


@dataclass(frozen=True)
class NixBase:
    """A Nix base: a VolumeSnapshot that the Nix base Job made for one sandbox image."""

    name: str
    # The label of the image digest (see `nix_base.image_digest_label`).
    image_digest: str
    # Seconds since the epoch.
    created: float
    ready: bool


@dataclass(frozen=True)
class Archive:
    """A sandbox archive: a VolumeSnapshot of the volume of a deleted sandbox (ticket 14)."""

    name: str
    # The name of the sandbox that the archive copies.
    sandbox: str
    # Seconds since the epoch. None when the label is missing or not a number: such an archive
    # never expires on its own.
    expires_at: float | None
    ready: bool


@dataclass(frozen=True)
class ExecResult:
    output: str
    exit_code: int


@dataclass
class CancelToken:
    """The caller sets this to stop a running exec."""

    _event: threading.Event = field(default_factory=threading.Event)

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


class Cluster(Protocol):
    """The port creates, finds, lists, waits for, runs in, marks, suspends and deletes a sandbox.
    It also lists and deletes the Nix bases, and makes, lists and deletes the sandbox archives.

    Each operation raises a `ClusterError` (or a subclass) when the cluster fails it.
    """

    def create_sandbox(
        self,
        name: str,
        *,
        image: str,
        labels: dict[str, str],
        profile: str = DEFAULT_PROFILE,
        nix_base: str | None = None,
        annotations: dict[str, str] | None = None,
    ) -> None:
        """Create the Sandbox. It mounts the Secret of the profile. Do nothing when it exists
        already. The `annotations` go on the Sandbox, not on its pod.

        When `nix_base` names a VolumeSnapshot, the volume of the sandbox is a clone of it.
        Otherwise the volume is empty.
        """
        ...

    def get_sandbox(self, name: str) -> SandboxStatus | None:
        """Return the status, or None when the Sandbox does not exist."""
        ...

    def wait_ready(self, name: str, *, timeout: float) -> SandboxStatus:
        """Wait for the Ready condition. Raise SandboxNotReady after `timeout` seconds."""
        ...

    def exec(
        self,
        name: str,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float,
        cancel: CancelToken,
    ) -> ExecResult:
        """Run `argv` in the pod of the Sandbox. Stderr joins the output.

        When `cancel` is set, return exit code 130. After `timeout` seconds, return exit code 124.
        The real cluster closes the stream but does not kill the remote process. The caller stops
        it with a second exec.
        Raise `SandboxNotFound` when the Sandbox does not exist, and `ClusterError` when it has no pod.
        """
        ...

    def set_operating_mode(self, name: str, mode: str) -> None:
        """Set `operatingMode` to RUNNING or SUSPENDED."""
        ...

    def list_sandboxes(self) -> list[SandboxStatus]:
        """Return the status of each Sandbox of the namespace."""
        ...

    def mark_used(self, name: str, at: float) -> None:
        """Record `at` (seconds since the epoch) as the last-use time of the Sandbox.

        Also set its `shutdownTime` to `at` plus SANDBOX_TTL_SECONDS. After that time the
        controller deletes the pod and starts no new one, so call this before a resume.
        """
        ...

    def delete_sandbox(self, name: str, *, resource_version: str | None = None) -> None:
        """Delete the Sandbox and its volume. Do nothing when it does not exist.

        With `resource_version`, the API server refuses the delete when the Sandbox changed
        since that version. The volume then stays, and a `ClusterError` is raised.
        """
        ...

    def list_nix_bases(self) -> list[NixBase]:
        """Return each Nix base in the namespace, ready or not."""
        ...

    def delete_nix_base(self, name: str) -> None:
        """Delete the Nix base. Do nothing when it does not exist."""
        ...

    def archive_sandbox(
        self, name: str, *, last_used: float, expires_at: float, timeout: float
    ) -> str:
        """Make a sandbox archive of the volume of the Sandbox and return its name.

        The name of the archive comes from the sandbox and `last_used`. When that archive exists,
        wait for it and make no second one. A later use of the sandbox gives a new archive.
        Return only when the archive is ready to use. Raise `ClusterError` when it is not ready
        after `timeout` seconds or when it failed, and `QuotaExceeded` when the quota refuses it.
        """
        ...

    def list_archives(self) -> list[Archive]:
        """Return each sandbox archive in the namespace, ready or not."""
        ...

    def delete_archive(self, name: str) -> None:
        """Delete the sandbox archive. Do nothing when it does not exist."""
        ...

"""The cluster port: the small interface between the plugin and Kubernetes.

The environment of the plugin talks only to this port. The `FakeCluster` keeps the
objects in memory for tests. A later ticket adds a client of the real API behind it.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass, field
from typing import Protocol

NAMESPACE = "hermes-sandbox"
RUNNING = "Running"
SUSPENDED = "Suspended"

# A Kubernetes object name is at most 63 characters (a DNS label).
_NAME_PREFIX = "hermes-"
_NAME_HASH_LENGTH = 40


def sandbox_name(task_id: str) -> str:
    """Name the sandbox of a task from a hash of its id.

    The same id gives the same name. The hash keeps the name valid for any id.
    """
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    return f"{_NAME_PREFIX}{digest[:_NAME_HASH_LENGTH]}"


class ClusterError(RuntimeError):
    """The cluster refused or failed an operation."""


class SandboxNotFound(ClusterError):
    """The sandbox does not exist."""


class SandboxNotReady(ClusterError):
    """The sandbox did not become ready before the deadline."""


@dataclass(frozen=True)
class SandboxStatus:
    """What the plugin reads from a Sandbox: its mode and its conditions."""

    name: str
    operating_mode: str
    ready: bool
    suspended: bool


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
    """Six operations are enough for the life of a sandbox."""

    def create_sandbox(self, name: str, *, image: str, labels: dict[str, str]) -> None:
        """Create the Sandbox. Do nothing when it exists already."""
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

        Return exit code 130 when `cancel` is set, and 124 after `timeout` seconds.
        """
        ...

    def set_operating_mode(self, name: str, mode: str) -> None:
        """Set `operatingMode` to RUNNING or SUSPENDED."""
        ...

    def delete_sandbox(self, name: str) -> None:
        """Delete the Sandbox and its volume. Do nothing when it does not exist."""
        ...

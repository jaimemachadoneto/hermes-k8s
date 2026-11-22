"""An in-memory cluster for tests.

It keeps the Sandbox objects in a dict. Its exec runs the `argv` as a real
process in a temporary directory, so the commands behave as they do in a pod. The names follow
what the agent-sandbox controller does: the pod has the name of the Sandbox, and
the volume is `data-<sandbox name>`.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .cluster import (
    RUNNING,
    SUSPENDED,
    CancelToken,
    ClusterError,
    ExecResult,
    SandboxNotFound,
    SandboxNotReady,
    SandboxStatus,
)
from .sandbox_spec import VOLUME

_POLL_SECONDS = 0.02
_TEMPLATE = VOLUME


@dataclass
class FakeSandbox:
    name: str
    image: str
    labels: dict[str, str]
    directory: Path
    operating_mode: str = RUNNING
    ready: bool = True

    @property
    def pod_name(self) -> str:
        return self.name

    @property
    def pvc_name(self) -> str:
        return f"{_TEMPLATE}-{self.name}"

    def status(self) -> SandboxStatus:
        suspended = self.operating_mode == SUSPENDED
        return SandboxStatus(
            self.name, self.operating_mode, self.ready and not suspended, suspended
        )


@dataclass
class FakeCluster:
    root: Path
    sandboxes: dict[str, FakeSandbox] = field(default_factory=dict)
    # Set it to make each new sandbox stay without a ready pod.
    never_ready: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create_sandbox(self, name: str, *, image: str, labels: dict[str, str]) -> None:
        with self._lock:
            if name in self.sandboxes:
                return
            directory = self.root / name
            directory.mkdir(parents=True)
            # A login shell resets PATH. The image of a real sandbox puts its tools back
            # in its profile, so the fake does the same for the tools of the test process.
            (directory / ".bash_profile").write_text(f"export PATH={os.environ.get('PATH', '')}\n")
            self.sandboxes[name] = FakeSandbox(
                name, image, dict(labels), directory, ready=not self.never_ready
            )

    def get_sandbox(self, name: str) -> SandboxStatus | None:
        sandbox = self.sandboxes.get(name)
        return sandbox.status() if sandbox else None

    def wait_ready(self, name: str, *, timeout: float) -> SandboxStatus:
        deadline = time.monotonic() + timeout
        while True:
            status = self.get_sandbox(name)
            if status is None:
                raise SandboxNotFound(name)
            if status.ready:
                return status
            if time.monotonic() >= deadline:
                raise SandboxNotReady(name)
            time.sleep(_POLL_SECONDS)

    def exec(
        self,
        name: str,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float,
        cancel: CancelToken,
    ) -> ExecResult:
        sandbox = self.sandboxes.get(name)
        if sandbox is None:
            raise SandboxNotFound(name)
        if not sandbox.status().ready:
            raise ClusterError(f"sandbox {name} has no running pod")
        # The pod gets no secret of the test process: only a path and a home.
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(sandbox.directory)}
        proc = subprocess.Popen(
            argv,
            cwd=sandbox.directory,
            env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        pending_input = stdin
        chunks: list[bytes] = []
        while True:
            try:
                out, _ = proc.communicate(input=pending_input, timeout=_POLL_SECONDS)
                chunks.append(out or b"")
                return ExecResult(_decode(chunks), proc.returncode)
            except subprocess.TimeoutExpired:
                pending_input = None  # communicate() keeps the input it already took
            if cancel.cancelled:
                return ExecResult(_decode(chunks) + _kill(proc), 130)
            if time.monotonic() >= deadline:
                return ExecResult(_decode(chunks) + _kill(proc), 124)

    def set_operating_mode(self, name: str, mode: str) -> None:
        sandbox = self.sandboxes.get(name)
        if sandbox is None:
            raise SandboxNotFound(name)
        sandbox.operating_mode = mode

    def delete_sandbox(self, name: str) -> None:
        with self._lock:
            self.sandboxes.pop(name, None)


def _kill(proc: subprocess.Popen) -> str:
    """Stop the process group, and return the output that it wrote before the stop."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    out, _ = proc.communicate()
    return out.decode("utf-8", "replace") if out else ""


def _decode(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", "replace")

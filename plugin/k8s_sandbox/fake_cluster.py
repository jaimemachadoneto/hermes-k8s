"""An in-memory cluster for tests.

It keeps the Sandbox objects in a dict. Its exec runs the `argv` as a real
process in a temporary directory, so the commands behave as they do in a pod. The names follow
what the agent-sandbox controller does: the pod has the name of the Sandbox, and
the volume is `data-<sandbox name>`.
"""

from __future__ import annotations

import os
import select
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
    NixBase,
    SandboxNotFound,
    SandboxNotReady,
    SandboxStatus,
)
from .sandbox_spec import DEFAULT_PROFILE, SECRET_PATH, SECRET_VOLUME, VOLUME, build_sandbox

_POLL_SECONDS = 0.02
_READ_BYTES = 65536
_TEMPLATE = VOLUME


@dataclass
class FakeSandbox:
    name: str
    image: str
    labels: dict[str, str]
    directory: Path
    operating_mode: str = RUNNING
    ready: bool = True
    last_used: float | None = None
    # The Secret that the Sandbox object mounts, and the path of the mount.
    secret_name: str = ""
    secret_mount_path: str = ""
    # The name of the Nix base that the volume was cloned from, or None for an empty volume.
    nix_base: str | None = None

    @property
    def pod_name(self) -> str:
        return self.name

    @property
    def pvc_name(self) -> str:
        return f"{_TEMPLATE}-{self.name}"

    def status(self) -> SandboxStatus:
        suspended = self.operating_mode == SUSPENDED
        return SandboxStatus(
            self.name, self.operating_mode, self.ready and not suspended, suspended, self.last_used
        )


@dataclass
class FakeCluster:
    root: Path
    sandboxes: dict[str, FakeSandbox] = field(default_factory=dict)
    # Set it to make each new sandbox stay without a ready pod.
    never_ready: bool = False
    # The Secrets of the namespace: name to files. A sandbox sees the files of its Secret in a
    # directory that stands for the mount path (the variable GITHUB_SECRETS_DIR).
    secrets: dict[str, dict[str, str]] = field(default_factory=dict)
    # The login script that the image of a real sandbox sets in BASH_ENV.
    bash_env: str = ""
    # Each exec request that the cluster received: the argv and the stdin.
    exec_log: list[tuple[list[str], bytes | None]] = field(default_factory=list)
    nix_bases: list[NixBase] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def create_sandbox(
        self,
        name: str,
        *,
        image: str,
        labels: dict[str, str],
        profile: str = DEFAULT_PROFILE,
        nix_base: str | None = None,
    ) -> None:
        # The real object decides which Secret the pod mounts.
        body = build_sandbox(name, image=image, labels=labels, profile=profile, nix_base=nix_base)
        pod = body["spec"]["podTemplate"]["spec"]
        secret = next(
            v["secret"]["secretName"] for v in pod["volumes"] if v["name"] == SECRET_VOLUME
        )
        mount = next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == SECRET_VOLUME)
        assert mount["readOnly"] and mount["mountPath"] == SECRET_PATH
        with self._lock:
            if name in self.sandboxes:
                return
            directory = self.root / name
            directory.mkdir(parents=True)
            # A login shell resets PATH. The image of a real sandbox puts its tools back
            # in its profile, so the fake does the same for the tools of the test process.
            (directory / ".bash_profile").write_text(f"export PATH={os.environ.get('PATH', '')}\n")
            self.sandboxes[name] = FakeSandbox(
                name,
                image,
                dict(labels),
                directory,
                ready=not self.never_ready,
                secret_name=secret,
                secret_mount_path=mount["mountPath"],
                nix_base=nix_base,
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
        self.exec_log.append((list(argv), stdin))
        files = self.secrets.get(sandbox.secret_name)
        if files is not None:
            mount = sandbox.directory.parent / f"{name}-secret"
            mount.mkdir(exist_ok=True)
            for key, value in files.items():
                (mount / key).write_text(value + "\n")
            env["GITHUB_SECRETS_DIR"] = str(mount)
        if self.bash_env:
            env["BASH_ENV"] = self.bash_env
        proc = subprocess.Popen(
            argv,
            cwd=sandbox.directory,
            env=env,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if stdin is not None:
            threading.Thread(target=_feed, args=(proc, stdin), daemon=True).start()
        try:
            return _collect(proc, timeout, cancel)
        finally:
            if proc.stdout is not None:
                proc.stdout.close()

    def set_operating_mode(self, name: str, mode: str) -> None:
        sandbox = self.sandboxes.get(name)
        if sandbox is None:
            raise SandboxNotFound(name)
        sandbox.operating_mode = mode

    def list_sandboxes(self) -> list[SandboxStatus]:
        return [sandbox.status() for sandbox in list(self.sandboxes.values())]

    def mark_used(self, name: str, at: float) -> None:
        with self._lock:
            sandbox = self.sandboxes.get(name)
            if sandbox is None:
                raise SandboxNotFound(name)
            sandbox.last_used = at

    def delete_sandbox(self, name: str) -> None:
        with self._lock:
            self.sandboxes.pop(name, None)

    def list_nix_bases(self) -> list[NixBase]:
        return list(self.nix_bases)

    def delete_nix_base(self, name: str) -> None:
        self.nix_bases = [b for b in self.nix_bases if b.name != name]


def _collect(proc: subprocess.Popen, timeout: float, cancel: CancelToken) -> ExecResult:
    """Read the output until the command exits, or until the timeout or a cancel."""
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    at_end = False
    while True:
        if not at_end and select.select([fd], [], [], _POLL_SECONDS)[0]:
            chunk = os.read(fd, _READ_BYTES)
            if chunk:
                chunks.append(chunk)
                continue
            at_end = True
        elif at_end:
            time.sleep(_POLL_SECONDS)
        # A real exec call ends when the command exits. A background job can hold the pipe
        # open, so do not wait for the end of the output. Read what the command wrote
        # before it exited.
        if proc.poll() is not None:
            while not at_end and select.select([fd], [], [], 0)[0]:
                chunk = os.read(fd, _READ_BYTES)
                if not chunk:
                    break
                chunks.append(chunk)
            return ExecResult(_decode(chunks), proc.returncode)
        if cancel.cancelled:
            return ExecResult(_decode(chunks) + _kill(proc), 130)
        if time.monotonic() >= deadline:
            return ExecResult(_decode(chunks) + _kill(proc), 124)


def _feed(proc: subprocess.Popen, data: bytes) -> None:
    """Write the input in a thread, so a large input cannot block the read of the output."""
    assert proc.stdin is not None
    try:
        proc.stdin.write(data)
    except BrokenPipeError:
        pass  # The command exited and did not read all of its input.
    finally:
        try:
            proc.stdin.close()
        except OSError, ValueError:
            pass


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

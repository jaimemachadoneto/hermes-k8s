"""An in-memory cluster for tests.

It keeps the Sandbox objects in a dict. Its exec runs the `argv` as a real
process in a temporary directory, so the commands behave as they do in a pod. The names follow
what the agent-sandbox controller does: the pod has the name of the Sandbox, and
the volume is `data-<sandbox name>`.
"""

from __future__ import annotations

import os
import select
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .cluster import (
    RUNNING,
    SANDBOX_TTL_SECONDS,
    SUSPENDED,
    TASK_LABEL,
    Archive,
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
    annotations: dict[str, str] = field(default_factory=dict)
    # The `shutdownTime` of the Sandbox (seconds since the epoch), or None.
    shutdown_time: float | None = None
    # The resourceVersion: each change of the object gives a new one.
    version: int = 1

    @property
    def pod_name(self) -> str:
        return self.name

    @property
    def pvc_name(self) -> str:
        return f"{_TEMPLATE}-{self.name}"

    def status(self) -> SandboxStatus:
        suspended = self.operating_mode == SUSPENDED
        return SandboxStatus(
            self.name,
            self.operating_mode,
            self.ready and not suspended,
            suspended,
            self.last_used,
            resource_version=str(self.version),
            managed=TASK_LABEL in self.labels,
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
    archives: dict[str, Archive] = field(default_factory=dict)
    # While it is False, no archive becomes ready.
    archives_ready: bool = True
    # The error that the archive of a sandbox raises, by the name of the sandbox.
    archive_error: dict[str, ClusterError] = field(default_factory=dict)
    # The error that the delete of an archive raises, by the name of the archive.
    delete_archive_error: dict[str, ClusterError] = field(default_factory=dict)
    # The error that `list_archives` raises, or None.
    list_archives_error: ClusterError | None = None
    # Functions that a test sets to change a sandbox at one step: they get the name.
    before_get: Callable[[str], None] | None = None
    during_archive: Callable[[str], None] | None = None
    before_delete: Callable[[str], None] | None = None
    # The archives, the deletes, the marks and the mode changes of the sandboxes, in order.
    events: list[tuple[str, ...]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add_sandbox(
        self,
        name: str,
        *,
        mode: str = RUNNING,
        last_used: float | None = None,
        managed: bool = True,
    ) -> None:
        """Add a sandbox with no files, as an earlier process (or another tool) left it."""
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        labels = {TASK_LABEL: name} if managed else {}
        self.sandboxes[name] = FakeSandbox(
            name, "", labels, directory, operating_mode=mode, last_used=last_used
        )

    def add_archive(
        self, name: str, *, sandbox: str, expires_at: float | None, ready: bool = True
    ) -> None:
        with self._lock:
            self.archives[name] = Archive(name, sandbox, expires_at, ready)

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
        # The real object decides which Secret the pod mounts.
        body = build_sandbox(
            name,
            image=image,
            labels=labels,
            profile=profile,
            nix_base=nix_base,
            annotations=annotations,
        )
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
            # A task that comes back after a delete gets the same name.
            directory.mkdir(parents=True, exist_ok=True)
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
                annotations=dict(body["metadata"].get("annotations", {})),
            )

    def get_sandbox(self, name: str) -> SandboxStatus | None:
        if self.before_get is not None:
            self.before_get(name)
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
            # Reap the process when it ends, so it leaves no zombie. Nothing kills it here.
            threading.Thread(target=proc.wait, daemon=True).start()

    def set_operating_mode(self, name: str, mode: str) -> None:
        sandbox = self.sandboxes.get(name)
        if sandbox is None:
            raise SandboxNotFound(name)
        sandbox.operating_mode = mode
        sandbox.version += 1
        self.events.append(("mode", name, mode))

    def list_sandboxes(self) -> list[SandboxStatus]:
        return [sandbox.status() for sandbox in list(self.sandboxes.values())]

    def mark_used(self, name: str, at: float) -> None:
        with self._lock:
            sandbox = self.sandboxes.get(name)
            if sandbox is None:
                raise SandboxNotFound(name)
            sandbox.last_used = at
            sandbox.shutdown_time = at + SANDBOX_TTL_SECONDS
            sandbox.version += 1
            self.events.append(("mark", name))

    def delete_sandbox(self, name: str, *, resource_version: str | None = None) -> None:
        if self.before_delete is not None:
            self.before_delete(name)
        with self._lock:
            sandbox = self.sandboxes.get(name)
            if (
                sandbox is not None
                and resource_version is not None
                and resource_version != str(sandbox.version)
            ):
                raise ClusterError(f"delete sandbox {name}: 409 the object has been modified")
            self.sandboxes.pop(name, None)
            self.events.append(("delete", name))

    def list_nix_bases(self) -> list[NixBase]:
        return list(self.nix_bases)

    def delete_nix_base(self, name: str) -> None:
        self.nix_bases = [b for b in self.nix_bases if b.name != name]

    def archive_sandbox(
        self, name: str, *, last_used: float, expires_at: float, timeout: float
    ) -> str:
        if name in self.archive_error:
            raise self.archive_error[name]
        archive_name = f"archive-{name}-{int(last_used)}"
        with self._lock:
            archive = self.archives.get(archive_name)
            if archive is None:
                if name not in self.sandboxes:
                    raise SandboxNotFound(name)
                archive = Archive(archive_name, name, expires_at, False)
                self.events.append(("archive", name))
            if self.archives_ready:
                archive = Archive(archive.name, archive.sandbox, archive.expires_at, True)
            self.archives[archive_name] = archive
        if self.during_archive is not None:
            self.during_archive(name)
        if not archive.ready:
            raise ClusterError(f"the archive {archive_name} is not ready after {timeout} seconds")
        return archive_name

    def list_archives(self) -> list[Archive]:
        if self.list_archives_error is not None:
            raise self.list_archives_error
        with self._lock:
            return list(self.archives.values())

    def delete_archive(self, name: str) -> None:
        if name in self.delete_archive_error:
            raise self.delete_archive_error[name]
        with self._lock:
            self.archives.pop(name, None)


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
        # The real client closes the stream and leaves the remote process alone. This fake does
        # the same. Only a second exec can stop the command.
        if cancel.cancelled:
            return ExecResult(_decode(chunks), 130)
        if time.monotonic() >= deadline:
            return ExecResult(_decode(chunks), 124)


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


def _decode(chunks: list[bytes]) -> str:
    return b"".join(chunks).decode("utf-8", "replace")

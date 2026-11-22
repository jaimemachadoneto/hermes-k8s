"""The Hermes environment: it runs each command with `bash -c` in the sandbox of a task."""

from __future__ import annotations

import logging

from tools.environments.base import BaseEnvironment
from tools.environments.base_output import _ThreadedProcessHandle

from .nix_base import choose_nix_base
from .cluster import RUNNING, SUSPENDED, CancelToken, Cluster, ClusterError, sandbox_name

logger = logging.getLogger(__name__)

READY_TIMEOUT_SECONDS = 300


class K8sSandboxEnvironment(BaseEnvironment):
    """One sandbox for one task id.

    The base class keeps the working directory and the exported variables between
    commands. Two environments of one task id share one sandbox, so call `cleanup` only
    when the session ends. It does not sync files with the Hermes home (ADR 0004): this class
    makes no `FileSyncManager`. It sends no secret with a command (ADR 0008).
    """

    # The base class puts the stdin of a command in a heredoc inside the command, so
    # the exec call needs no stdin channel.
    _stdin_mode = "heredoc"
    # cleanup() suspends the sandbox: the processes in it stop with the pod.
    cleanup_stops_processes = True

    def __init__(
        self,
        *,
        cluster: Cluster,
        task_id: str,
        image: str,
        cwd: str,
        timeout: int,
        ready_timeout: float = READY_TIMEOUT_SECONDS,
    ):
        super().__init__(cwd=cwd, timeout=timeout)
        self._cluster = cluster
        self._task_id = task_id
        self._ready_timeout = ready_timeout
        self.sandbox_name = sandbox_name(task_id)
        self._open_sandbox(image)
        if cwd == "~":
            self.cwd = self._resolve_home(cwd)
        self.init_session()

    def _open_sandbox(self, image: str) -> None:
        """Create the sandbox, or resume the one of the same task id."""
        status = self._cluster.get_sandbox(self.sandbox_name)
        if status is None:
            nix_base = choose_nix_base(self._cluster, image)
            self._cluster.create_sandbox(
                self.sandbox_name,
                image=image,
                labels={"hermes-task": self.sandbox_name},
                nix_base=nix_base,
            )
            logger.info("Created sandbox %s from Nix base %s", self.sandbox_name, nix_base)
        elif status.operating_mode == SUSPENDED:
            self._cluster.set_operating_mode(self.sandbox_name, RUNNING)
            logger.info("Resumed sandbox %s", self.sandbox_name)
        self._cluster.wait_ready(self.sandbox_name, timeout=self._ready_timeout)

    def _resolve_home(self, cwd: str) -> str:
        result = self._cluster.exec(
            self.sandbox_name,
            ["bash", "-c", 'printf %s "$HOME"'],
            timeout=30,
            cancel=CancelToken(),
        )
        home = result.output.strip()
        if result.exit_code != 0 or not home:
            raise ClusterError(f"cannot read the home of sandbox {self.sandbox_name}")
        return home

    def _run_bash(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        cancel = CancelToken()
        argv = ["bash", *(["-l"] if login else []), "-c", cmd_string]

        def exec_fn() -> tuple[str, int]:
            try:
                result = self._cluster.exec(self.sandbox_name, argv, timeout=timeout, cancel=cancel)
            except ClusterError as error:
                # The handle of Hermes turns an exception into exit code 1 with no text.
                logger.warning("exec in sandbox %s failed: %s", self.sandbox_name, error)
                return f"sandbox error: {error}", 1
            return result.output, result.exit_code

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel.cancel)

    def cleanup(self) -> None:
        """Suspend the sandbox. Its volume stays for the next environment of the task."""
        try:
            self._cluster.set_operating_mode(self.sandbox_name, SUSPENDED)
        except Exception:
            logger.warning("Could not suspend sandbox %s", self.sandbox_name, exc_info=True)

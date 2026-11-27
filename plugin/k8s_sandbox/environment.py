"""The Hermes environment: it runs each command with `bash -c` in the sandbox of a task."""

from __future__ import annotations

import logging
import time
import uuid

from tools.environments.base import BaseEnvironment
from tools.environments.base_output import _ThreadedProcessHandle

from .cluster import (
    PROFILE_ANNOTATION,
    RUNNING,
    SUSPENDED,
    TASK_ANNOTATION,
    CancelToken,
    Cluster,
    ClusterError,
    sandbox_name,
)
from .lifecycle import Clock, UseMarker
from .nix_base import choose_nix_base
from .sandbox_spec import DEFAULT_PROFILE

logger = logging.getLogger(__name__)

READY_TIMEOUT_SECONDS = 300
# Exit codes of the cluster client for a timeout and for a cancel.
_TIMEOUT_EXIT_CODE = 124
_CANCEL_EXIT_CODE = 130
STOP_TIMEOUT_SECONDS = 30

# The command runs as a job of a shell with job control. The job is a process group of its own.
# Its group id is in a file. The file is in a directory that only this command uses. The shell
# removes the directory when the job ends. Arguments: the id of the command, "1" for a login
# shell or "0" for another shell, and the command text.
RUN_SCRIPT = """\
set -m
umask 077
d=/tmp/hermes-k8s/$1
mkdir -p "$d"
if [ "$2" = 1 ]; then bash -l -c "$3" & else bash -c "$3" & fi
pgid=$!
echo "$pgid" > "$d/pgid"
wait "$pgid" 2>/dev/null
rc=$?
rm -rf "$d"
exit "$rc"
"""

# A command that has ended has no directory, so this script then does nothing. It sends TERM to
# the group, waits up to 2 seconds, and then sends KILL. Argument: the id of the command.
STOP_SCRIPT = """\
d=/tmp/hermes-k8s/$1
[ -d "$d" ] || exit 0
# The run script writes the file a moment after the job starts. Wait for it.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -s "$d/pgid" ] && break
  sleep 0.1
done
read -r pgid < "$d/pgid" 2>/dev/null || exit 0
case $pgid in ''|*[!0-9]*|0|1) exit 0 ;; esac
kill -TERM -- "-$pgid" 2>/dev/null
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  kill -0 -- "-$pgid" 2>/dev/null || break
  sleep 0.1
done
if kill -0 -- "-$pgid" 2>/dev/null; then
  kill -KILL -- "-$pgid" 2>/dev/null
  sleep 0.2
  if kill -0 -- "-$pgid" 2>/dev/null; then echo "group $pgid is still alive" >&2; exit 1; fi
fi
rm -rf "$d"
"""


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
    # Hermes reads this flag. A persistent environment survives the end of a turn, and the idle
    # reaper of Hermes calls cleanup() after `terminal.lifetime_seconds`. Without the flag, Hermes
    # calls cleanup() after each turn, and the sandbox is suspended while the user reads the answer.
    _persistent = True

    def __init__(
        self,
        *,
        cluster: Cluster,
        task_id: str,
        image: str,
        cwd: str,
        timeout: int,
        profile: str = DEFAULT_PROFILE,
        ready_timeout: float = READY_TIMEOUT_SECONDS,
        extra_labels: dict[str, str] | None = None,
        clock: Clock = time.time,
    ):
        super().__init__(cwd=cwd, timeout=timeout)
        self._cluster = cluster
        self._task_id = task_id
        self._ready_timeout = ready_timeout
        self.sandbox_name = sandbox_name(task_id, profile)
        self._extra_labels = extra_labels or {}
        self._use = UseMarker(cluster, self.sandbox_name, clock)
        self._open_sandbox(image, profile)
        if cwd == "~":
            self.cwd = self._resolve_home(cwd)
        self.init_session()

    def _open_sandbox(self, image: str, profile: str) -> None:
        """Create the sandbox, or resume the one of the same task id."""
        status = self._cluster.get_sandbox(self.sandbox_name)
        if status is None:
            # An existing sandbox keeps its labels. A changed allowance applies only to a new sandbox.
            nix_base = choose_nix_base(self._cluster, image)
            self._cluster.create_sandbox(
                self.sandbox_name,
                image=image,
                labels={"hermes-task": self.sandbox_name, **self._extra_labels},
                profile=profile,
                nix_base=nix_base,
                annotations={TASK_ANNOTATION: self._task_id, PROFILE_ANNOTATION: profile},
            )
            logger.info(
                "Created sandbox %s with %s",
                self.sandbox_name,
                f"Nix base {nix_base}" if nix_base else "an empty volume",
            )
        else:
            # After `shutdownTime` the controller starts no pod, also in Running mode. The new
            # last use moves `shutdownTime`, so it must come before the resume.
            self._use.mark(force=True)
            if status.operating_mode == SUSPENDED:
                self._cluster.set_operating_mode(self.sandbox_name, RUNNING)
                logger.info("Resumed sandbox %s", self.sandbox_name)
        self._cluster.wait_ready(self.sandbox_name, timeout=self._ready_timeout)
        self._use.mark(force=True)

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
        command_id = uuid.uuid4().hex
        argv = ["bash", "-c", RUN_SCRIPT, "k8s-run", command_id, "1" if login else "0", cmd_string]

        def exec_fn() -> tuple[str, int]:
            # Mark at the start and at the end, so a long command does not look idle.
            self._use.mark()
            try:
                result = self._cluster.exec(self.sandbox_name, argv, timeout=timeout, cancel=cancel)
            except ClusterError as error:
                # The handle of Hermes turns an exception into exit code 1 with no text.
                logger.warning("exec in sandbox %s failed: %s", self.sandbox_name, error)
                return f"sandbox error: {error}", 1
            if result.exit_code in (_TIMEOUT_EXIT_CODE, _CANCEL_EXIT_CODE):
                self._stop_command(command_id, result.exit_code)
            self._use.mark()
            return result.output, result.exit_code

        return _ThreadedProcessHandle(exec_fn, cancel_fn=cancel.cancel)

    def _stop_command(self, command_id: str, exit_code: int) -> None:
        """Stop the command in the sandbox after a timeout or a cancel.

        The exec request ends on the client side, but the remote command continues. A second
        exec stops its process group. Exit code 124 can also come from the command itself:
        the stop then finds no directory and does nothing. The caller keeps its exit code.
        """
        try:
            result = self._cluster.exec(
                self.sandbox_name,
                ["bash", "-c", STOP_SCRIPT, "k8s-stop", command_id],
                timeout=STOP_TIMEOUT_SECONDS,
                cancel=CancelToken(),
            )
        except ClusterError as error:
            logger.warning(
                "could not stop command %s (exit %s) in sandbox %s: %s",
                command_id,
                exit_code,
                self.sandbox_name,
                error,
            )
            return
        if result.exit_code != 0:
            logger.warning(
                "the stop of command %s (exit %s) in sandbox %s gave exit code %s: %s",
                command_id,
                exit_code,
                self.sandbox_name,
                result.exit_code,
                result.output.strip(),
            )

    def cleanup(self) -> None:
        """Suspend the sandbox. Its volume stays for the next environment of the task.

        Hermes calls this at the end of a session, after the idle time, and at shutdown. It never
        deletes the sandbox. The reaper in `archive.py` deletes a sandbox after 7 days.
        """
        try:
            self._use.mark(force=True)
            self._cluster.set_operating_mode(self.sandbox_name, SUSPENDED)
        except Exception:
            logger.warning("Could not suspend sandbox %s", self.sandbox_name, exc_info=True)

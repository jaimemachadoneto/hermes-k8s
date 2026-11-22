"""The cluster port over the Python `kubernetes` client.

The client authenticates with a header, so no token goes in an exec URL. This module logs
no command and no body of a response. A command goes to the pod as one list of arguments, so
the API does the quoting and a command cannot change the list.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import urllib3.exceptions
import websocket
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream

from .cluster import (
    SUSPENDED,
    CancelToken,
    ClusterError,
    ExecResult,
    QuotaExceeded,
    SandboxNotFound,
    SandboxNotReady,
    SandboxStatus,
)
from .sandbox_spec import API_VERSION, NAMESPACE, SHELL, VOLUME, build_sandbox

logger = logging.getLogger(__name__)

GROUP, VERSION = API_VERSION.split("/")
PLURAL = "sandboxes"
_POLL_SECONDS = 1.0
_EXEC_POLL_SECONDS = 0.1
_QUOTA = re.compile(r"exceeded quota: ([\w.-]+)")


def _api_message(error: ApiException) -> str:
    """The message of the API server, without the headers and the raw body."""
    try:
        return str(json.loads(error.body or "{}").get("message") or error.reason)
    except ValueError:
        return str(error.reason)


def _quota_error(message: str) -> QuotaExceeded | None:
    match = _QUOTA.search(message)
    if match is None:
        return None
    return QuotaExceeded(
        f"the quota {match.group(1)} of the namespace {NAMESPACE} is full, "
        f"so the cluster cannot start a sandbox: {message}"
    )


# A dropped connection is a cluster failure too. The port promises a ClusterError.
_NETWORK_ERRORS = (OSError, urllib3.exceptions.HTTPError, websocket.WebSocketException)


def _map(error: ApiException, what: str) -> ClusterError:
    message = _api_message(error)
    quota = _quota_error(message) if error.status == 403 else None
    if quota is not None:
        return quota
    if error.status == 404:
        return SandboxNotFound(f"{what}: not found")
    return ClusterError(f"{what}: {error.status} {message}")


class KubernetesCluster:
    """Implements `Cluster` with the Sandbox custom resource and `pods/exec`."""

    def __init__(
        self,
        *,
        custom: Any,
        core: Any,
        open_exec: Callable[[str, list[str]], Any],
        poll_seconds: float = _POLL_SECONDS,
    ):
        self._custom = custom
        self._core = core
        self._open_exec = open_exec
        self._poll = poll_seconds

    @classmethod
    def from_kubeconfig(cls, path: str | None = None) -> KubernetesCluster:
        """Return a cluster client. If `path` is None, use the account of the pod.
        Otherwise use the kubeconfig file at `path`."""
        configuration = client.Configuration()
        if path is None:
            config.load_incluster_config(client_configuration=configuration)
        else:
            config.load_kube_config(config_file=path, client_configuration=configuration)

        def new_api() -> client.ApiClient:
            return client.ApiClient(configuration)

        def open_exec(name: str, argv: list[str]):
            # `stream` replaces a method of the client during the call. Make a new client for each call.
            core = client.CoreV1Api(new_api())
            return stream(
                core.connect_get_namespaced_pod_exec,
                name,
                NAMESPACE,
                container=SHELL,
                command=argv,
                stdin=False,
                stdout=True,
                stderr=True,
                tty=False,
                _preload_content=False,
            )

        api = new_api()
        return cls(
            custom=client.CustomObjectsApi(api), core=client.CoreV1Api(api), open_exec=open_exec
        )

    def create_sandbox(
        self, name: str, *, image: str, labels: dict[str, str], profile: str = "default"
    ) -> None:
        body = build_sandbox(name, image=image, labels=labels, profile=profile)
        try:
            self._custom.create_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL, body)
        except ApiException as error:
            if error.status == 409:
                return
            raise _map(error, f"create sandbox {name}") from None

    def _read(self, name: str) -> dict | None:
        try:
            return self._custom.get_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, PLURAL, name
            )
        except ApiException as error:
            if error.status == 404:
                return None
            raise _map(error, f"read sandbox {name}") from None
        except _NETWORK_ERRORS as error:
            raise ClusterError(f"read sandbox {name}: {type(error).__name__}") from None

    @staticmethod
    def _status(name: str, item: dict) -> SandboxStatus:
        mode = item.get("spec", {}).get("operatingMode") or "Running"
        suspended = mode == SUSPENDED
        ready = any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in item.get("status", {}).get("conditions", [])
        )
        return SandboxStatus(name, mode, ready and not suspended, suspended)

    def get_sandbox(self, name: str) -> SandboxStatus | None:
        item = self._read(name)
        return None if item is None else self._status(name, item)

    def _pod_ready(self, name: str) -> bool:
        """The Sandbox can say Ready from before a resume. The pod is the proof."""
        try:
            pod = self._core.read_namespaced_pod(name, NAMESPACE)
        except ApiException as error:
            if error.status == 404:
                return False
            raise _map(error, f"read pod {name}") from None
        if pod.metadata.deletion_timestamp is not None:
            return False
        return any(c.type == "Ready" and c.status == "True" for c in (pod.status.conditions or []))

    def wait_ready(self, name: str, *, timeout: float) -> SandboxStatus:
        deadline = time.monotonic() + timeout
        while True:
            item = self._read(name)
            if item is None:
                raise SandboxNotFound(name)
            status = self._status(name, item)
            if status.ready and self._pod_ready(name):
                return status
            for condition in item.get("status", {}).get("conditions", []):
                quota = _quota_error(str(condition.get("message", "")))
                if quota is not None:
                    raise quota
            if time.monotonic() >= deadline:
                raise SandboxNotReady(f"sandbox {name} is not ready after {timeout:.0f} seconds")
            time.sleep(self._poll)

    def exec(
        self,
        name: str,
        argv: list[str],
        *,
        stdin: bytes | None = None,
        timeout: float,
        cancel: CancelToken,
    ) -> ExecResult:
        if stdin is not None:
            raise ClusterError("stdin is not supported: the environment sends it in the command")
        try:
            socket = self._open_exec(name, argv)
        except ApiException as error:
            raise _map(error, f"exec in sandbox {name}") from None
        except _NETWORK_ERRORS as error:
            raise ClusterError(f"exec in sandbox {name}: {type(error).__name__}") from None
        deadline = time.monotonic() + timeout
        chunks: list[str] = []
        try:
            while socket.is_open():
                socket.update(timeout=_EXEC_POLL_SECONDS)
                if socket.peek_stdout():
                    chunks.append(socket.read_stdout())
                if socket.peek_stderr():
                    chunks.append(socket.read_stderr())
                if cancel.cancelled:
                    return ExecResult("".join(chunks), 130)
                if time.monotonic() >= deadline:
                    return ExecResult("".join(chunks), 124)
            # The socket is closed. Read what the last frames left.
            while socket.peek_stdout():
                chunks.append(socket.read_stdout())
            while socket.peek_stderr():
                chunks.append(socket.read_stderr())
            try:
                code = socket.returncode
            except Exception as error:
                raise ClusterError(f"exec in sandbox {name} gave no exit code") from error
            if code is None:
                raise ClusterError(f"exec in sandbox {name} gave no exit code")
            return ExecResult("".join(chunks), code)
        except _NETWORK_ERRORS as error:
            raise ClusterError(f"exec in sandbox {name}: {type(error).__name__}") from None
        finally:
            # Close the socket on each path. The remote process of a command that is cancelled
            # or timed out is not killed here. It stops when the sandbox is suspended.
            socket.close()

    def set_operating_mode(self, name: str, mode: str) -> None:
        try:
            self._custom.patch_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, PLURAL, name, {"spec": {"operatingMode": mode}}
            )
        except ApiException as error:
            raise _map(error, f"set the mode of sandbox {name}") from None

    def delete_sandbox(self, name: str) -> None:
        for call, what in (
            (
                lambda: self._custom.delete_namespaced_custom_object(
                    GROUP, VERSION, NAMESPACE, PLURAL, name
                ),
                f"delete sandbox {name}",
            ),
            (
                lambda: self._core.delete_namespaced_persistent_volume_claim(
                    f"{VOLUME}-{name}", NAMESPACE
                ),
                f"delete the volume of sandbox {name}",
            ),
        ):
            try:
                call()
            except ApiException as error:
                if error.status != 404:
                    raise _map(error, what) from None

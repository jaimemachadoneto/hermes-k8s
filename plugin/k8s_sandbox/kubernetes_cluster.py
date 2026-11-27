"""The cluster port over the Python `kubernetes` client.

The client authenticates with a header, so no token goes in an exec URL. This module logs
no command and no body of a response. A command goes to the pod as one list of arguments, so
the API does the quoting and a command cannot change the list.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

import urllib3.exceptions
import websocket
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from kubernetes.stream import stream

from .cluster import (
    LAST_USED_ANNOTATION,
    SANDBOX_TTL_SECONDS,
    SUSPENDED,
    TASK_LABEL,
    Archive,
    CancelToken,
    ClusterError,
    ExecResult,
    NixBase,
    QuotaExceeded,
    SandboxNotFound,
    SandboxNotReady,
    SandboxStatus,
)
from .nix_base import KIND, LABEL_DIGEST, LABEL_KIND
from .sandbox_spec import API_VERSION, DEFAULT_PROFILE, NAMESPACE, SHELL, VOLUME, build_sandbox

logger = logging.getLogger(__name__)

GROUP, VERSION = API_VERSION.split("/")
PLURAL = "sandboxes"
SNAPSHOT_GROUP, SNAPSHOT_VERSION, SNAPSHOT_PLURAL = (
    "snapshot.storage.k8s.io",
    "v1",
    "volumesnapshots",
)
# The class of the Nix bases too (the Nix base Job of home-ops).
SNAPSHOT_CLASS = "csi-ceph-blockpool"
# The labels of a sandbox archive. The kind label is the one of the Nix bases, so the two lists
# never mix.
ARCHIVE_KIND = "sandbox-archive"
LABEL_SANDBOX = "hermes-k8s/sandbox"
LABEL_EXPIRES = "hermes-k8s/expires"
ARCHIVE_PREFIX = "archive-"
_POLL_SECONDS = 1.0
_EXEC_POLL_SECONDS = 0.1
_QUOTA = re.compile(r"exceeded quota: ([\w.-]+)")


def _api_message(error: ApiException) -> str:
    """The message of the API server, without the headers and the raw body."""
    try:
        return str(json.loads(error.body or "{}").get("message") or error.reason)
    except ValueError:
        return str(error.reason)


def _quota_error(message: str, what: str) -> QuotaExceeded | None:
    match = _QUOTA.search(message)
    if match is None:
        return None
    return QuotaExceeded(
        f"the quota {match.group(1)} of the namespace {NAMESPACE} is full, "
        f"so the cluster refuses to {what}: {message}"
    )


def _volume_name(name: str) -> str:
    """The PVC of a sandbox: the controller names it from the volume template and the Sandbox."""
    return f"{VOLUME}-{name}"


def _rfc3339(at: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


# A dropped connection is a cluster failure too. The port promises a ClusterError.
_NETWORK_ERRORS = (OSError, urllib3.exceptions.HTTPError, websocket.WebSocketException)


def _map(error: ApiException, what: str) -> ClusterError:
    message = _api_message(error)
    quota = _quota_error(message, what) if error.status == 403 else None
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
        self,
        name: str,
        *,
        image: str,
        labels: dict[str, str],
        profile: str = DEFAULT_PROFILE,
        nix_base: str | None = None,
        annotations: dict[str, str] | None = None,
    ) -> None:
        body = build_sandbox(
            name,
            image=image,
            labels=labels,
            profile=profile,
            nix_base=nix_base,
            annotations=annotations,
        )
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
        metadata = item.get("metadata", {})
        stamp = (metadata.get("annotations") or {}).get(LAST_USED_ANNOTATION)
        try:
            last_used = float(stamp) if stamp is not None else None
        except ValueError:
            last_used = None
        if last_used is not None and not math.isfinite(last_used):
            last_used = None
        return SandboxStatus(
            name,
            mode,
            ready and not suspended,
            suspended,
            last_used,
            resource_version=metadata.get("resourceVersion"),
            managed=TASK_LABEL in (metadata.get("labels") or {}),
        )

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
                quota = _quota_error(str(condition.get("message", "")), f"start sandbox {name}")
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
            # or timed out is not killed here. The caller stops it with a second exec.
            socket.close()

    def set_operating_mode(self, name: str, mode: str) -> None:
        try:
            self._custom.patch_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, PLURAL, name, {"spec": {"operatingMode": mode}}
            )
        except ApiException as error:
            raise _map(error, f"set the mode of sandbox {name}") from None

    def list_sandboxes(self) -> list[SandboxStatus]:
        try:
            result = self._custom.list_namespaced_custom_object(GROUP, VERSION, NAMESPACE, PLURAL)
        except ApiException as error:
            raise _map(error, "list the sandboxes") from None
        except _NETWORK_ERRORS as error:
            raise ClusterError(f"list the sandboxes: {type(error).__name__}") from None
        return [self._status(i["metadata"]["name"], i) for i in result.get("items", [])]

    def mark_used(self, name: str, at: float) -> None:
        body = {
            "metadata": {"annotations": {LAST_USED_ANNOTATION: repr(at)}},
            "spec": {"shutdownTime": _rfc3339(at + SANDBOX_TTL_SECONDS)},
        }
        try:
            self._custom.patch_namespaced_custom_object(
                GROUP, VERSION, NAMESPACE, PLURAL, name, body
            )
        except ApiException as error:
            raise _map(error, f"mark the use of sandbox {name}") from None
        except _NETWORK_ERRORS as error:
            raise ClusterError(f"mark the use of sandbox {name}: {type(error).__name__}") from None

    def delete_sandbox(self, name: str, *, resource_version: str | None = None) -> None:
        options = {}
        if resource_version is not None:
            options["body"] = client.V1DeleteOptions(
                preconditions=client.V1Preconditions(resource_version=resource_version)
            )
        # A refused precondition raises before the second call, so the volume stays.
        for call, what in (
            (
                lambda: self._custom.delete_namespaced_custom_object(
                    GROUP, VERSION, NAMESPACE, PLURAL, name, **options
                ),
                f"delete sandbox {name}",
            ),
            (
                lambda: self._core.delete_namespaced_persistent_volume_claim(
                    _volume_name(name), NAMESPACE
                ),
                f"delete the volume of sandbox {name}",
            ),
        ):
            try:
                call()
            except ApiException as error:
                if error.status != 404:
                    raise _map(error, what) from None

    def list_nix_bases(self) -> list[NixBase]:
        try:
            result = self._custom.list_namespaced_custom_object(
                SNAPSHOT_GROUP,
                SNAPSHOT_VERSION,
                NAMESPACE,
                SNAPSHOT_PLURAL,
                label_selector=f"{LABEL_KIND}={KIND}",
            )
        except ApiException as error:
            raise _map(error, "list the Nix bases") from None
        except _NETWORK_ERRORS as error:
            raise ClusterError(f"list the Nix bases: {type(error).__name__}") from None
        return [
            NixBase(
                name=item["metadata"]["name"],
                image_digest=item["metadata"].get("labels", {}).get(LABEL_DIGEST, ""),
                created=datetime.fromisoformat(item["metadata"]["creationTimestamp"]).timestamp(),
                ready=bool(item.get("status", {}).get("readyToUse")),
            )
            for item in result.get("items", [])
            # The CSI driver keeps a snapshot that has a clone. Do not select it.
            if not item["metadata"].get("deletionTimestamp")
        ]

    def delete_nix_base(self, name: str) -> None:
        try:
            self._custom.delete_namespaced_custom_object(
                SNAPSHOT_GROUP, SNAPSHOT_VERSION, NAMESPACE, SNAPSHOT_PLURAL, name
            )
        except ApiException as error:
            if error.status != 404:
                raise _map(error, f"delete the Nix base {name}") from None

    def archive_sandbox(
        self, name: str, *, last_used: float, expires_at: float, timeout: float
    ) -> str:
        # One name for one sandbox and one last use: a second reaper finds the same archive,
        # and a later use of the sandbox gets a new one.
        archive = f"{ARCHIVE_PREFIX}{name}-{int(last_used)}"
        body = {
            "apiVersion": f"{SNAPSHOT_GROUP}/{SNAPSHOT_VERSION}",
            "kind": "VolumeSnapshot",
            "metadata": {
                "name": archive,
                "namespace": NAMESPACE,
                "labels": {
                    LABEL_KIND: ARCHIVE_KIND,
                    LABEL_SANDBOX: name,
                    LABEL_EXPIRES: str(int(expires_at)),
                },
            },
            "spec": {
                "volumeSnapshotClassName": SNAPSHOT_CLASS,
                "source": {"persistentVolumeClaimName": _volume_name(name)},
            },
        }
        what = f"archive sandbox {name}"
        try:
            self._custom.create_namespaced_custom_object(
                SNAPSHOT_GROUP, SNAPSHOT_VERSION, NAMESPACE, SNAPSHOT_PLURAL, body
            )
        except ApiException as error:
            # 409: the archive of this last use exists. Wait for it.
            if error.status != 409:
                raise _map(error, what) from None
        except _NETWORK_ERRORS as error:
            raise ClusterError(f"{what}: {type(error).__name__}") from None
        deadline = time.monotonic() + timeout
        while True:
            try:
                item = self._custom.get_namespaced_custom_object(
                    SNAPSHOT_GROUP, SNAPSHOT_VERSION, NAMESPACE, SNAPSHOT_PLURAL, archive
                )
            except ApiException as error:
                raise _map(error, what) from None
            except _NETWORK_ERRORS as error:
                raise ClusterError(f"{what}: {type(error).__name__}") from None
            status = item.get("status") or {}
            if status.get("readyToUse"):
                return archive
            failure = status.get("error")
            if failure:
                # The snapshotter can retry a failure, but the next run makes the archive again
                # in any case. The delete frees the quota now. The error goes first into the log,
                # so a failed delete does not hide it.
                message = failure.get("message") if isinstance(failure, dict) else failure
                logger.warning("The archive %s failed: %s", archive, message)
                self.delete_archive(archive)
                raise ClusterError(f"the archive {archive} failed: {message}")
            if time.monotonic() >= deadline:
                raise ClusterError(
                    f"the archive {archive} is not ready after {timeout:.0f} seconds"
                )
            time.sleep(self._poll)

    def list_archives(self) -> list[Archive]:
        try:
            result = self._custom.list_namespaced_custom_object(
                SNAPSHOT_GROUP,
                SNAPSHOT_VERSION,
                NAMESPACE,
                SNAPSHOT_PLURAL,
                label_selector=f"{LABEL_KIND}={ARCHIVE_KIND}",
            )
        except ApiException as error:
            raise _map(error, "list the sandbox archives") from None
        except _NETWORK_ERRORS as error:
            raise ClusterError(f"list the sandbox archives: {type(error).__name__}") from None
        archives = []
        for item in result.get("items", []):
            # The reaper skips an archive that is in deletion.
            if item["metadata"].get("deletionTimestamp"):
                continue
            labels = item["metadata"].get("labels", {})
            try:
                expires_at: float | None = float(labels.get(LABEL_EXPIRES, ""))
            except ValueError:
                expires_at = None
            if expires_at is not None and not math.isfinite(expires_at):
                expires_at = None
            archives.append(
                Archive(
                    name=item["metadata"]["name"],
                    sandbox=labels.get(LABEL_SANDBOX, ""),
                    expires_at=expires_at,
                    ready=bool((item.get("status") or {}).get("readyToUse")),
                )
            )
        return archives

    def delete_archive(self, name: str) -> None:
        try:
            self._custom.delete_namespaced_custom_object(
                SNAPSHOT_GROUP, SNAPSHOT_VERSION, NAMESPACE, SNAPSHOT_PLURAL, name
            )
        except ApiException as error:
            if error.status != 404:
                raise _map(error, f"delete the sandbox archive {name}") from None

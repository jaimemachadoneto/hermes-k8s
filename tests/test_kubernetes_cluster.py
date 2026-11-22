"""The real cluster port against stubs of the Kubernetes API: what we send and how errors map."""

import json

import pytest
from k8s_sandbox.cluster import (
    CancelToken,
    ClusterError,
    QuotaExceeded,
    SandboxNotFound,
    SandboxNotReady,
)
from k8s_sandbox.kubernetes_cluster import KubernetesCluster
from kubernetes.client.exceptions import ApiException

QUOTA_BODY = {
    "kind": "Status",
    "status": "Failure",
    "reason": "Forbidden",
    "message": (
        'sandboxes.agents.x-k8s.io "hermes-a" is forbidden: exceeded quota: hermes-sandbox-quota, '
        "requested: count/sandboxes.agents.x-k8s.io=1, used: count/sandboxes.agents.x-k8s.io=20, "
        "limited: count/sandboxes.agents.x-k8s.io=20"
    ),
}


def api_error(status, body):
    return ApiException(status=status, reason="x", body=json.dumps(body))


def sandbox_object(*, mode="Running", ready="True", message=""):
    return {
        "metadata": {"name": "hermes-a"},
        "spec": {"operatingMode": mode},
        "status": {"conditions": [{"type": "Ready", "status": ready, "message": message}]},
    }


class StubCustom:
    def __init__(self):
        self.objects, self.calls, self.fail = {}, [], None

    def create_namespaced_custom_object(self, group, version, namespace, plural, body, **kw):
        self.calls.append(("create", group, version, namespace, plural, body))
        if self.fail:
            raise self.fail
        self.objects[body["metadata"]["name"]] = body
        return body

    def get_namespaced_custom_object(self, group, version, namespace, plural, name, **kw):
        if name not in self.objects:
            raise api_error(404, {"message": "not found"})
        item = self.objects[name]
        return item() if callable(item) else item

    def patch_namespaced_custom_object(self, group, version, namespace, plural, name, body, **kw):
        self.calls.append(("patch", name, body))
        if name not in self.objects:
            raise api_error(404, {"message": "not found"})

    def delete_namespaced_custom_object(self, group, version, namespace, plural, name, **kw):
        self.calls.append(("delete", name))
        if name not in self.objects:
            raise api_error(404, {"message": "not found"})
        del self.objects[name]


class StubCore:
    def __init__(self):
        self.pod_ready, self.pvcs, self.deleted = True, {"data-hermes-a"}, []

    def read_namespaced_pod(self, name, namespace, **kw):
        ready = "True" if self.pod_ready else "False"
        return type(
            "Pod",
            (),
            {
                "metadata": type("M", (), {"deletion_timestamp": None})(),
                "status": type(
                    "S", (), {"conditions": [type("C", (), {"type": "Ready", "status": ready})()]}
                )(),
            },
        )()

    def delete_namespaced_persistent_volume_claim(self, name, namespace, **kw):
        self.deleted.append(name)
        if name not in self.pvcs:
            raise api_error(404, {"message": "not found"})


class FakeSocket:
    """A stand-in for the websocket of the exec call."""

    def __init__(self, stdout=(), stderr=(), code: int | None = 0, stays_open=False):
        self._out, self._err, self._code = list(stdout), list(stderr), code
        self._stays_open, self.closed = stays_open, False

    def update(self, timeout=0):
        pass

    def peek_stdout(self, timeout=0):
        return bool(self._out)

    def read_stdout(self, timeout=0):
        return self._out.pop(0)

    def peek_stderr(self, timeout=0):
        return bool(self._err)

    def read_stderr(self, timeout=0):
        return self._err.pop(0)

    def is_open(self):
        return self._stays_open and not self.closed or bool(self._out or self._err)

    @property
    def returncode(self):
        return None if self.is_open() else self._code

    def close(self):
        self.closed = True


@pytest.fixture
def custom():
    return StubCustom()


@pytest.fixture
def core():
    return StubCore()


@pytest.fixture
def make(custom, core):
    sockets = {}

    def build(socket=None):
        def open_exec(name, argv):
            sockets["argv"] = argv
            if socket is None:
                raise api_error(404, {"message": "pod not found"})
            return socket

        return KubernetesCluster(
            custom=custom, core=core, open_exec=open_exec, poll_seconds=0.01
        ), sockets

    return build


IMAGE = "ghcr.io/jaimemachadoneto/hermes-sandbox@sha256:" + "0" * 64


def test_create_sends_the_sandbox_to_the_namespace_hermes_sandbox(make, custom):
    cluster, _ = make()

    cluster.create_sandbox("hermes-a", image=IMAGE, labels={"hermes-task": "hermes-a"})

    ((_, group, version, namespace, plural, body),) = custom.calls
    assert (group, version, namespace, plural) == (
        "agents.x-k8s.io",
        "v1beta1",
        "hermes-sandbox",
        "sandboxes",
    )
    assert body["metadata"]["name"] == "hermes-a"
    assert body["spec"]["podTemplate"]["spec"]["containers"][0]["image"] == IMAGE


def test_create_does_nothing_when_the_sandbox_exists(make, custom):
    cluster, _ = make()
    custom.fail = api_error(409, {"reason": "AlreadyExists", "message": "exists"})

    cluster.create_sandbox("hermes-a", image=IMAGE, labels={})

    assert custom.objects == {}


def test_a_full_quota_gives_an_error_that_names_the_quota(make, custom):
    cluster, _ = make()
    custom.fail = api_error(403, QUOTA_BODY)

    with pytest.raises(QuotaExceeded) as caught:
        cluster.create_sandbox("hermes-a", image=IMAGE, labels={})

    assert "hermes-sandbox-quota" in str(caught.value)
    assert "count/sandboxes.agents.x-k8s.io" in str(caught.value)
    assert isinstance(caught.value, ClusterError)


def test_a_refusal_by_the_admission_policy_gives_its_message(make, custom):
    cluster, _ = make()
    message = "ValidatingAdmissionPolicy 'hermes-sandbox-pod' denied request: runs the image"
    custom.fail = api_error(422, {"message": message})

    with pytest.raises(ClusterError, match="denied request"):
        cluster.create_sandbox("hermes-a", image=IMAGE, labels={})


def test_a_forbidden_error_that_is_not_a_quota_is_not_called_a_quota(make, custom):
    cluster, _ = make()
    custom.fail = api_error(403, {"message": "sandboxes is forbidden: User cannot create"})

    with pytest.raises(ClusterError) as caught:
        cluster.create_sandbox("hermes-a", image=IMAGE, labels={})

    assert not isinstance(caught.value, QuotaExceeded)


def test_get_returns_none_for_a_missing_sandbox_and_the_status_for_a_found_one(make, custom):
    cluster, _ = make()
    assert cluster.get_sandbox("hermes-a") is None

    custom.objects["hermes-a"] = sandbox_object(mode="Suspended", ready="False")
    status = cluster.get_sandbox("hermes-a")
    assert status and status.suspended and not status.ready
    assert status.operating_mode == "Suspended"

    custom.objects["hermes-a"] = sandbox_object()
    status = cluster.get_sandbox("hermes-a")
    assert status and status.ready and not status.suspended


def test_wait_ready_returns_when_the_sandbox_and_its_pod_are_ready(make, custom, core):
    cluster, _ = make()
    answers = iter([sandbox_object(ready="False"), sandbox_object(ready="False"), sandbox_object()])
    custom.objects["hermes-a"] = lambda: next(answers)

    assert cluster.wait_ready("hermes-a", timeout=5).ready


def test_wait_ready_does_not_trust_a_ready_sandbox_whose_pod_is_not_ready(make, custom, core):
    cluster, _ = make()
    custom.objects["hermes-a"] = sandbox_object()
    core.pod_ready = False

    with pytest.raises(SandboxNotReady):
        cluster.wait_ready("hermes-a", timeout=0.1)


def test_wait_ready_raises_not_found_for_a_missing_sandbox(make):
    cluster, _ = make()

    with pytest.raises(SandboxNotFound):
        cluster.wait_ready("hermes-a", timeout=0.1)


def test_wait_ready_stops_early_and_names_the_quota_when_the_controller_hits_it(make, custom):
    cluster, _ = make()
    message = 'pods "hermes-a" is forbidden: exceeded quota: hermes-sandbox-quota, limited: pods=6'
    custom.objects["hermes-a"] = sandbox_object(ready="False", message=message)

    with pytest.raises(QuotaExceeded, match="hermes-sandbox-quota"):
        cluster.wait_ready("hermes-a", timeout=30)


def test_exec_returns_the_output_with_stderr_and_the_exit_code(make):
    cluster, sockets = make(FakeSocket(stdout=["a\n"], stderr=["oops\n"], code=3))

    result = cluster.exec("hermes-a", ["bash", "-c", "x"], timeout=5, cancel=CancelToken())

    assert (result.output, result.exit_code) == ("a\noops\n", 3)
    assert sockets["argv"] == ["bash", "-c", "x"]


def test_exec_ends_with_124_at_the_timeout_and_with_130_at_a_cancel(make):
    slow = FakeSocket(stdout=[], stays_open=True)
    cluster, _ = make(slow)
    assert cluster.exec("hermes-a", ["x"], timeout=0.05, cancel=CancelToken()).exit_code == 124
    assert slow.closed

    cancel = CancelToken()
    cancel.cancel()
    other = FakeSocket(stays_open=True)
    cluster, _ = make(other)
    assert cluster.exec("hermes-a", ["x"], timeout=5, cancel=cancel).exit_code == 130
    assert other.closed


def test_exec_raises_not_found_when_the_pod_is_missing(make):
    cluster, _ = make()

    with pytest.raises(SandboxNotFound):
        cluster.exec("hermes-a", ["x"], timeout=5, cancel=CancelToken())


def test_exec_refuses_standard_input_because_the_environment_sends_it_in_the_command(make):
    cluster, _ = make(FakeSocket())

    with pytest.raises(ClusterError, match="stdin"):
        cluster.exec("hermes-a", ["x"], stdin=b"x", timeout=5, cancel=CancelToken())


def test_set_operating_mode_patches_only_that_field(make, custom):
    cluster, _ = make()
    custom.objects["hermes-a"] = sandbox_object()

    cluster.set_operating_mode("hermes-a", "Suspended")

    assert custom.calls == [("patch", "hermes-a", {"spec": {"operatingMode": "Suspended"}})]


def test_set_operating_mode_raises_not_found_for_a_missing_sandbox(make):
    cluster, _ = make()

    with pytest.raises(SandboxNotFound):
        cluster.set_operating_mode("hermes-a", "Suspended")


def test_delete_removes_the_sandbox_and_its_volume_and_ignores_what_is_gone(make, custom, core):
    cluster, _ = make()
    custom.objects["hermes-a"] = sandbox_object()

    cluster.delete_sandbox("hermes-a")
    cluster.delete_sandbox("hermes-a")

    assert "hermes-a" not in custom.objects
    assert core.deleted == ["data-hermes-a", "data-hermes-a"]


def test_a_suspended_sandbox_with_a_stale_ready_condition_is_not_ready(make, custom):
    cluster, _ = make()
    custom.objects["hermes-a"] = sandbox_object(mode="Suspended", ready="True")

    status = cluster.get_sandbox("hermes-a")

    assert status and not status.ready and status.suspended


def test_a_sandbox_with_no_mode_runs(make, custom):
    cluster, _ = make()
    item = sandbox_object()
    del item["spec"]["operatingMode"]
    custom.objects["hermes-a"] = item

    status = cluster.get_sandbox("hermes-a")

    assert status and status.operating_mode == "Running" and status.ready


def test_delete_ignores_a_volume_that_does_not_exist(make, custom, core):
    cluster, _ = make()
    custom.objects["hermes-b"] = sandbox_object()

    cluster.delete_sandbox("hermes-b")

    assert core.deleted == ["data-hermes-b"]


def test_delete_and_set_mode_and_exec_raise_cluster_error_on_a_server_error(make, custom):
    cluster, _ = make()
    custom.objects["hermes-a"] = sandbox_object()
    custom.delete_namespaced_custom_object = lambda *a, **k: (_ for _ in ()).throw(
        api_error(500, {"message": "boom"})
    )
    custom.patch_namespaced_custom_object = custom.delete_namespaced_custom_object

    with pytest.raises(ClusterError, match="boom"):
        cluster.delete_sandbox("hermes-a")
    with pytest.raises(ClusterError, match="boom"):
        cluster.set_operating_mode("hermes-a", "Running")

    def failing_exec(name, argv):
        raise api_error(500, {"message": "boom"})

    failing = KubernetesCluster(custom=custom, core=None, open_exec=failing_exec)
    with pytest.raises(ClusterError) as caught:
        failing.exec("hermes-a", ["x"], timeout=1, cancel=CancelToken())
    assert not isinstance(caught.value, SandboxNotFound)


def test_a_dropped_connection_gives_a_cluster_error(make, custom):
    import urllib3.exceptions

    def broken_exec(name, argv):
        raise urllib3.exceptions.ProtocolError("reset")

    cluster = KubernetesCluster(custom=custom, core=None, open_exec=broken_exec)
    with pytest.raises(ClusterError, match="ProtocolError"):
        cluster.exec("hermes-a", ["x"], timeout=1, cancel=CancelToken())

    custom.get_namespaced_custom_object = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
    with pytest.raises(ClusterError, match="OSError"):
        cluster.get_sandbox("hermes-a")


def test_exec_raises_when_the_socket_closes_without_an_exit_code(make):
    cluster, _ = make(FakeSocket(stdout=[], code=None))

    with pytest.raises(ClusterError, match="exit code"):
        cluster.exec("hermes-a", ["x"], timeout=5, cancel=CancelToken())


def test_exec_keeps_the_output_that_arrives_with_the_close_frame(make):
    class ClosedWithOutput(FakeSocket):
        def is_open(self):
            return False

    cluster, _ = make(ClosedWithOutput(stdout=["last\n"], stderr=["err\n"], code=0))

    result = cluster.exec("hermes-a", ["x"], timeout=5, cancel=CancelToken())

    assert (result.output, result.exit_code) == ("last\nerr\n", 0)


def test_wait_ready_does_not_accept_a_pod_that_is_being_deleted(make, custom, core):
    cluster, _ = make()
    custom.objects["hermes-a"] = sandbox_object()
    pod = core.read_namespaced_pod("hermes-a", "x")
    pod.metadata.deletion_timestamp = "2026-01-01T00:00:00Z"
    core.read_namespaced_pod = lambda *a, **k: pod

    with pytest.raises(SandboxNotReady):
        cluster.wait_ready("hermes-a", timeout=0.1)


def test_an_error_with_a_body_that_is_not_json_still_gives_a_cluster_error(make, custom):
    cluster, _ = make()
    custom.fail = ApiException(status=500, reason="boom", body="<html>")

    with pytest.raises(ClusterError, match="boom"):
        cluster.create_sandbox("hermes-a", image=IMAGE, labels={})

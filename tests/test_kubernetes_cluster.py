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
        self.objects, self.calls, self.fail, self.snapshots = {}, [], None, []

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

    def list_namespaced_custom_object(self, group, version, namespace, plural, **kw):
        if plural == "volumesnapshots":
            self.calls.append(("list", group, version, namespace, plural, kw))
            if self.fail:
                raise self.fail
            return {"items": self.snapshots}
        if self.fail:
            raise self.fail
        return {"items": [i() if callable(i) else i for i in self.objects.values()]}

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


def snapshot_object(name, digest, created, *, ready=True):
    return {
        "metadata": {
            "name": name,
            "creationTimestamp": created,
            "labels": {"hermes-k8s/kind": "nix-base", "hermes-k8s/image-digest": digest},
        },
        "status": {"readyToUse": ready},
    }


def test_the_nix_bases_are_the_volume_snapshots_with_the_label_nix_base(make, custom):
    cluster, _ = make()
    custom.snapshots = [
        snapshot_object("a", "d" * 32, "2026-11-22T10:00:00Z"),
        snapshot_object("b", "e" * 32, "2026-11-22T11:00:00Z", ready=False),
    ]

    bases = cluster.list_nix_bases()

    ((_, group, version, namespace, plural, kw),) = custom.calls
    assert (group, version, namespace, plural) == (
        "snapshot.storage.k8s.io",
        "v1",
        "hermes-sandbox",
        "volumesnapshots",
    )
    assert kw["label_selector"] == "hermes-k8s/kind=nix-base"
    assert [(b.name, b.image_digest, b.ready) for b in bases] == [
        ("a", "d" * 32, True),
        ("b", "e" * 32, False),
    ]
    assert bases[1].created - bases[0].created == 3600


def test_a_snapshot_with_no_status_is_not_ready(make, custom):
    cluster, _ = make()
    item = snapshot_object("a", "d" * 32, "2026-11-22T10:00:00Z")
    del item["status"]
    custom.snapshots = [item]

    assert [b.ready for b in cluster.list_nix_bases()] == [False]


def test_a_refused_list_of_the_nix_bases_is_a_cluster_error(make, custom):
    cluster, _ = make()
    custom.fail = api_error(403, {"message": "forbidden"})

    with pytest.raises(ClusterError, match="forbidden"):
        cluster.list_nix_bases()


def test_delete_nix_base_deletes_the_snapshot_and_ignores_a_missing_one(make, custom):
    cluster, _ = make()
    custom.objects["a"] = {}

    cluster.delete_nix_base("a")
    cluster.delete_nix_base("a")

    assert custom.calls == [("delete", "a"), ("delete", "a")]


def test_a_snapshot_that_is_being_deleted_is_not_a_nix_base(make, custom):
    cluster, _ = make()
    item = snapshot_object("a", "d" * 32, "2026-11-22T10:00:00Z")
    item["metadata"]["deletionTimestamp"] = "2026-11-22T11:00:00Z"
    custom.snapshots = [item, snapshot_object("b", "d" * 32, "2026-11-22T10:00:00Z")]

    assert [b.name for b in cluster.list_nix_bases()] == ["b"]


def test_a_refused_delete_of_a_nix_base_is_a_cluster_error(make, custom):
    cluster, _ = make()
    custom.objects["a"] = {}
    custom.fail = None

    def refuse(*args, **kw):
        raise api_error(403, {"message": "forbidden"})

    custom.delete_namespaced_custom_object = refuse

    with pytest.raises(ClusterError, match="forbidden"):
        cluster.delete_nix_base("a")


def test_create_without_a_nix_base_sends_no_data_source(make, custom):
    cluster, _ = make()

    cluster.create_sandbox("hermes-a", image=IMAGE, labels={})

    (call,) = custom.calls
    assert "dataSource" not in call[5]["spec"]["volumeClaimTemplates"][0]["spec"]


def test_create_with_a_nix_base_sends_the_data_source(make, custom):
    cluster, _ = make()

    cluster.create_sandbox("hermes-a", image=IMAGE, labels={}, nix_base="nix-base-1")

    (call,) = custom.calls
    template = call[5]["spec"]["volumeClaimTemplates"][0]
    assert template["spec"]["dataSource"] == {
        "apiGroup": "snapshot.storage.k8s.io",
        "kind": "VolumeSnapshot",
        "name": "nix-base-1",
    }


def test_the_status_reads_the_last_use_annotation_as_a_float(make, custom):
    item = sandbox_object()
    item["metadata"]["annotations"] = {"hermes-k8s/last-used": "123.5"}
    custom.objects["hermes-a"] = item

    assert make()[0].get_sandbox("hermes-a").last_used == 123.5


@pytest.mark.parametrize("annotations", [None, {}, {"hermes-k8s/last-used": "abc"}])
def test_a_missing_or_bad_last_use_annotation_gives_none(make, custom, annotations):
    item = sandbox_object()
    item["metadata"]["annotations"] = annotations
    custom.objects["hermes-a"] = item

    assert make()[0].get_sandbox("hermes-a").last_used is None


def test_list_gives_one_status_for_each_sandbox(make, custom):
    custom.objects["hermes-a"] = sandbox_object()
    custom.objects["hermes-b"] = {
        **sandbox_object(mode="Suspended"),
        "metadata": {"name": "hermes-b"},
    }

    statuses = {s.name: s.operating_mode for s in make()[0].list_sandboxes()}

    assert statuses == {"hermes-a": "Running", "hermes-b": "Suspended"}


def test_mark_used_patches_the_annotation_and_the_end_of_life(make, custom):
    custom.objects["hermes-a"] = sandbox_object()

    make()[0].mark_used("hermes-a", 1_800_000_000.5)

    # The controller stops the pod 7 days after the last use (ADR 0013).
    assert custom.calls[-1] == (
        "patch",
        "hermes-a",
        {
            "metadata": {"annotations": {"hermes-k8s/last-used": "1800000000.5"}},
            "spec": {"shutdownTime": "2027-01-22T08:00:00Z"},
        },
    )


def test_list_and_mark_raise_cluster_error_on_a_failure(make, custom):
    custom.objects["hermes-a"] = sandbox_object()
    custom.fail = api_error(500, {"message": "boom"})
    with pytest.raises(ClusterError):
        make()[0].list_sandboxes()
    with pytest.raises(SandboxNotFound):
        make()[0].mark_used("missing", 1.0)
    custom.fail = OSError("reset")
    with pytest.raises(ClusterError):
        make()[0].list_sandboxes()


# --- the end of life and the sandbox archive (ticket 14, ADR 0013) -----------------------------


def test_create_keeps_the_sandbox_after_its_end_of_life_and_annotates_only_the_sandbox(
    make, custom
):
    cluster, _ = make()
    cluster.create_sandbox(
        "hermes-a",
        image=IMAGE,
        labels={"hermes-task": "hermes-a"},
        annotations={"hermes-k8s/task": "session:abc", "hermes-k8s/profile": "default"},
    )
    body = custom.objects["hermes-a"]
    # The plugin archives the volume before a delete, so the controller must not delete it.
    assert body["spec"]["shutdownPolicy"] == "Retain"
    assert body["metadata"]["annotations"] == {
        "hermes-k8s/task": "session:abc",
        "hermes-k8s/profile": "default",
    }
    # The pod gets no annotation: the admission policy and the network policies read the pod.
    assert "annotations" not in body["spec"]["podTemplate"]["metadata"]


def test_the_status_tells_a_sandbox_of_the_plugin_and_gives_its_version(make, custom):
    ours = sandbox_object()
    ours["metadata"].update(labels={"hermes-task": "hermes-a"}, resourceVersion="42")
    other = sandbox_object()
    other["metadata"] = {"name": "other", "resourceVersion": "7"}
    custom.objects = {"hermes-a": ours, "other": other}

    statuses = {s.name: s for s in make()[0].list_sandboxes()}

    assert statuses["hermes-a"].managed and statuses["hermes-a"].resource_version == "42"
    assert not statuses["other"].managed


def test_delete_with_a_version_sends_it_as_a_precondition(make, custom, core):
    cluster, _ = make()
    sent = []
    custom.delete_namespaced_custom_object = lambda *a, **kw: sent.append((a[-1], kw))

    cluster.delete_sandbox("hermes-a", resource_version="42")

    [(name, kw)] = sent
    assert name == "hermes-a"
    assert kw["body"].preconditions.resource_version == "42"
    assert core.deleted == ["data-hermes-a"]


def test_a_refused_precondition_keeps_the_volume(make, custom, core):
    cluster, _ = make()
    custom.objects["hermes-a"] = sandbox_object()

    def conflict(group, version, namespace, plural, name, **kw):
        raise api_error(409, {"message": "the object has been modified"})

    custom.delete_namespaced_custom_object = conflict

    with pytest.raises(ClusterError, match="modified"):
        cluster.delete_sandbox("hermes-a", resource_version="42")
    assert core.deleted == []


def snapshot_with_status(body, status):
    return {**body, "status": status}


def test_archive_snapshots_the_volume_and_waits_until_it_is_ready(make, custom):
    cluster, _ = make()
    polls = []

    def create(group, version, namespace, plural, body, **kw):
        custom.calls.append(("create", group, version, namespace, plural, body))

        def item():
            polls.append(1)
            return snapshot_with_status(body, {"readyToUse": len(polls) >= 3})

        custom.objects[body["metadata"]["name"]] = item
        return body

    custom.create_namespaced_custom_object = create

    name = cluster.archive_sandbox(
        "hermes-a", last_used=1_000_000.7, expires_at=2_000_000.0, timeout=5
    )

    ((_, group, version, namespace, plural, body),) = custom.calls
    assert (group, version, namespace, plural) == (
        "snapshot.storage.k8s.io",
        "v1",
        "hermes-sandbox",
        "volumesnapshots",
    )
    # One name for one sandbox and one last use: a second reaper finds the same archive.
    assert name == body["metadata"]["name"] == "archive-hermes-a-1000000"
    assert body["metadata"]["labels"] == {
        "hermes-k8s/kind": "sandbox-archive",
        "hermes-k8s/sandbox": "hermes-a",
        "hermes-k8s/expires": "2000000",
    }
    assert body["spec"] == {
        "volumeSnapshotClassName": "csi-ceph-blockpool",
        "source": {"persistentVolumeClaimName": "data-hermes-a"},
    }
    assert len(polls) == 3


def test_an_archive_that_exists_is_waited_for_and_not_made_again(make, custom):
    cluster, _ = make()
    custom.objects["archive-hermes-a-1000000"] = {"status": {"readyToUse": True}}

    def exists(group, version, namespace, plural, body, **kw):
        custom.calls.append(("create", body["metadata"]["name"]))
        raise api_error(409, {"message": "already exists"})

    custom.create_namespaced_custom_object = exists

    name = cluster.archive_sandbox("hermes-a", last_used=1_000_000.0, expires_at=2e6, timeout=1)

    assert name == "archive-hermes-a-1000000"


def test_a_failed_archive_is_deleted_and_raises_at_once(make, custom):
    cluster, _ = make()

    def create(group, version, namespace, plural, body, **kw):
        custom.objects[body["metadata"]["name"]] = snapshot_with_status(
            body, {"readyToUse": False, "error": {"message": "rbd: cannot snapshot"}}
        )
        return body

    custom.create_namespaced_custom_object = create

    with pytest.raises(ClusterError, match="cannot snapshot"):
        cluster.archive_sandbox("hermes-a", last_used=1_000_000.0, expires_at=2e6, timeout=60)
    assert ("delete", "archive-hermes-a-1000000") in custom.calls


def test_an_archive_that_is_not_ready_in_time_is_a_cluster_error(make, custom):
    cluster, _ = make()

    with pytest.raises(ClusterError, match="not ready"):
        cluster.archive_sandbox("hermes-a", last_used=1_000_000.0, expires_at=2e6, timeout=0.05)


def test_a_full_snapshot_quota_names_the_archive_in_the_error(make, custom):
    cluster, _ = make()
    custom.fail = api_error(
        403,
        {"message": "volumesnapshots is forbidden: exceeded quota: hermes-sandbox-quota"},
    )

    with pytest.raises(QuotaExceeded) as caught:
        cluster.archive_sandbox("hermes-a", last_used=1_000_000.0, expires_at=2e6, timeout=1)
    assert "archive sandbox hermes-a" in str(caught.value)
    assert "start a sandbox" not in str(caught.value)


@pytest.mark.parametrize(
    "error", [api_error(500, {"message": "boom"}), OSError("connection reset")]
)
def test_a_failed_poll_of_the_archive_is_a_cluster_error(make, custom, error):
    cluster, _ = make()

    def broken(*a, **kw):
        raise error

    custom.get_namespaced_custom_object = broken

    with pytest.raises(ClusterError):
        cluster.archive_sandbox("hermes-a", last_used=1_000_000.0, expires_at=2e6, timeout=1)


def archive_object(name, sandbox, expires, *, ready=True, deleting=False):
    item = {
        "metadata": {
            "name": name,
            "creationTimestamp": "2026-11-27T10:00:00Z",
            "labels": {
                "hermes-k8s/kind": "sandbox-archive",
                "hermes-k8s/sandbox": sandbox,
                "hermes-k8s/expires": expires,
            },
        },
        "status": {"readyToUse": ready},
    }
    if deleting:
        item["metadata"]["deletionTimestamp"] = "2026-11-27T11:00:00Z"
    return item


def test_the_archives_are_the_volume_snapshots_with_the_label_sandbox_archive(make, custom):
    cluster, _ = make()
    custom.snapshots = [
        archive_object("x", "hermes-a", "2000000"),
        archive_object("y", "hermes-b", "not-a-number", ready=False),
        archive_object("n", "hermes-c", "nan"),
        archive_object("i", "hermes-c", "inf"),
        archive_object("gone", "hermes-d", "2000000", deleting=True),
    ]

    archives = cluster.list_archives()

    ((_, *_, kw),) = custom.calls
    assert kw["label_selector"] == "hermes-k8s/kind=sandbox-archive"
    # An archive with no valid expiry never expires on its own: a person examines it. An
    # archive in deletion is not an archive any more.
    assert [(a.name, a.sandbox, a.expires_at, a.ready) for a in archives] == [
        ("x", "hermes-a", 2_000_000.0, True),
        ("y", "hermes-b", None, False),
        ("n", "hermes-c", None, True),
        ("i", "hermes-c", None, True),
    ]


@pytest.mark.parametrize("error", [api_error(500, {"message": "boom"}), OSError("reset")])
def test_a_failed_list_of_the_archives_is_a_cluster_error(make, custom, error):
    cluster, _ = make()
    custom.fail = error

    with pytest.raises(ClusterError):
        cluster.list_archives()


def test_delete_archive_deletes_the_snapshot_and_ignores_a_missing_one(make, custom):
    cluster, _ = make()
    custom.objects["x"] = {}

    cluster.delete_archive("x")
    cluster.delete_archive("x")

    assert custom.calls == [("delete", "x"), ("delete", "x")]

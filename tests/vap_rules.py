"""A Python copy of the rules of the admission policy `hermes-sandbox-pod` of home-ops.

The source is `validatingadmissionpolicy.yaml` in home-ops. `fixtures/sandbox-good.yaml` is a
copy of `tests/hermes-sandbox/sandbox-good.yaml` there. Change this file with the policy.
The names of the rules: account, labels, host, scheduling, volume, template, init, image, pull-secret and
affinity. The quantities: only whole `Mi`, `Gi` and `Ti` values (the plugin sends `20Gi`).
"""

import re

IMAGE_PATTERN = re.compile(
    r"ghcr\.io/jaimemachadoneto/hermes-sandbox"
    r"(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?@sha256:[a-f0-9]{64}"
)
_SCHEDULING_FIELDS = (
    "tolerations",
    "nodeSelector",
    "priorityClassName",
    "runtimeClassName",
    "schedulerName",
    "shareProcessNamespace",
)
# The labels of the pod that the policy accepts: the task label, and one allowance label for each
# service that has a CiliumNetworkPolicy in home-ops. A new service needs a new entry here, in the
# policy and in its own policy.
ALLOWED_LABELS = frozenset({"hermes-task", "hermes-k8s/allow-litellm"})
_GIB = 1024**3


def _gibibytes(quantity: str) -> float:
    match = re.fullmatch(r"(\d+)(Gi|Mi|Ti)?", quantity)
    assert match, quantity
    factor = {None: 1 / _GIB, "Mi": 1 / 1024, "Gi": 1, "Ti": 1024}[match.group(2)]
    return int(match.group(1)) * factor


def violations(sandbox: dict) -> list[str]:
    """Return the names of the rules that the Sandbox breaks. An empty list means accepted."""
    spec = sandbox["spec"]
    pod = spec["podTemplate"]["spec"]
    found = []

    if pod.get("serviceAccountName") != "hermes-sandbox" or (
        pod.get("automountServiceAccountToken") is not False
    ):
        found.append("account")
    if "nodeName" in pod or any(pod.get(k) for k in ("hostNetwork", "hostPID", "hostIPC")):
        found.append("host")
    if any(k in pod for k in _SCHEDULING_FIELDS):
        found.append("scheduling")
    for volume in pod.get("volumes", []):
        empty_dir = volume.get("emptyDir")
        if empty_dir is None or "sizeLimit" not in empty_dir or empty_dir.get("medium", ""):
            found.append("volume")
    for template in spec.get("volumeClaimTemplates", []):
        if not _template_ok(template.get("spec", {})):
            found.append("template")
    init = pod.get("initContainers", [])
    if "ephemeralContainers" in pod or (
        "initContainers" in pod and (len(init) != 1 or init[0]["name"] != "copy-closure")
    ):
        found.append("init")
    for container in pod["containers"] + init:
        if (
            not IMAGE_PATTERN.fullmatch(container["image"])
            or any("valueFrom" in e for e in container.get("env", []))
            or "envFrom" in container
        ):
            found.append("image")
    if any(s["name"] != "ghcr-pull" for s in pod.get("imagePullSecrets", [])):
        found.append("pull-secret")
    labels = spec["podTemplate"].get("metadata", {}).get("labels", {})
    if not set(labels) <= ALLOWED_LABELS:
        found.append("labels")
    if not _affinity_ok(pod):
        found.append("affinity")
    return sorted(set(found))


def _template_ok(spec: dict) -> bool:
    source = spec.get("dataSource")
    reference = spec.get("dataSourceRef")
    snapshot = ("snapshot.storage.k8s.io", "VolumeSnapshot")
    storage = spec.get("resources", {}).get("requests", {}).get("storage")
    return (
        spec.get("storageClassName") == "ceph-block"
        and "volumeName" not in spec
        and "selector" not in spec
        and (source is None or (source.get("apiGroup"), source["kind"]) == snapshot)
        and (
            reference is None
            or (
                (reference.get("apiGroup"), reference["kind"]) == snapshot
                and "namespace" not in reference
            )
        )
        and spec.get("volumeMode", "Filesystem") == "Filesystem"
        and spec.get("accessModes") == ["ReadWriteOnce"]
        and storage is not None
        and _gibibytes(storage) <= 20
    )


def _affinity_ok(pod: dict) -> bool:
    node = pod.get("affinity", {}).get("nodeAffinity", {})
    required = node.get("requiredDuringSchedulingIgnoredDuringExecution")
    if required is None:
        return False
    return all(
        any(
            e["key"] == "kubernetes.io/hostname"
            and e["operator"] == "NotIn"
            and "home-ops-03" in e.get("values", [])
            for e in term.get("matchExpressions", [])
        )
        for term in required["nodeSelectorTerms"]
    )

"""The one function that builds a Sandbox. The admission policy `hermes-sandbox-pod` of
home-ops accepts only this shape. `tests/vap_rules.py` holds a copy of its rules.
"""

from __future__ import annotations

import re

NAMESPACE = "hermes-sandbox"
# The name of the volume claim template. The controller names the claim `<template>-<sandbox>`.
VOLUME = "data"
API_VERSION = "agents.x-k8s.io/v1beta1"
SHELL = "shell"
INIT = "copy-closure"
UID = 10000

# The policy pins the image by digest, so a tag that moves cannot change a sandbox.
_DIGEST = re.compile(r".+@sha256:[a-f0-9]{64}")

# The volume at /nix hides the store of the image (ADR 0003). The init step copies the store
# into the volume. The marker `.closure-copied` shows that a copy is complete. If the volume
# has the marker, it is a Nix base or an earlier copy, and the step does nothing. If the
# volume has no marker, the step removes the incomplete copy first. The store is read-only,
# so run `chmod` before `rm`. The workspace and the home are in the volume too. `mkdir -p`
# keeps the files that a Nix base has.
# The Job that builds a Nix base writes the marker too (`nix-base-build` in the image).
# Nix knows a store path only when its database lists the path. The image has the list of its
# own paths at /etc/nix/registration, and the step loads it into the database of the volume.
# Without it, Nix downloads each path of the image again.
_COPY_CLOSURE = """\
set -eu
mkdir -p /data/nix /data/workspace /data/home
if [ ! -e /data/nix/.closure-copied ]; then
  if [ -e /data/nix/store ]; then
    chmod -R u+w /data/nix/store
    rm -rf /data/nix/store
  fi
  cp -a --no-preserve=ownership /nix/store /data/nix/store
  NIX_STATE_DIR=/data/nix/var/nix NIX_LOG_DIR=/data/nix/var/log/nix \\
    nix-store --load-db < /etc/nix/registration
  touch /data/nix/.closure-copied
fi
"""

# A VolumeSnapshot name has at most 253 characters: lowercase letters, digits, '-' and '.'.
# It starts and ends with a letter or a digit.
_SNAPSHOT_NAME = re.compile(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?")


def validate_image(image: str) -> None:
    """Raise ValueError unless the image reference has a sha256 digest."""
    if not _DIGEST.fullmatch(image):
        raise ValueError(f"the sandbox image must have a sha256 digest: {image!r}")


def _security_context() -> dict:
    return {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "capabilities": {"drop": ["ALL"]},
    }


def _volume_spec(nix_base: str | None) -> dict:
    spec = {
        "accessModes": ["ReadWriteOnce"],
        "storageClassName": "ceph-block",
        # The size of a Nix base. A clone cannot be smaller than its snapshot, and the policy
        # allows 20Gi at most.
        "resources": {"requests": {"storage": "20Gi"}},
    }
    if nix_base is not None:
        if not _SNAPSHOT_NAME.fullmatch(nix_base):
            raise ValueError(f"the name of the Nix base is not a valid name: {nix_base!r}")
        spec["dataSource"] = {
            "apiGroup": "snapshot.storage.k8s.io",
            "kind": "VolumeSnapshot",
            "name": nix_base,
        }
    return spec


def build_sandbox(
    name: str, *, image: str, labels: dict[str, str], nix_base: str | None = None
) -> dict:
    """Return the Sandbox object. The object holds no secret and no token.

    With `nix_base`, the volume is a clone of that VolumeSnapshot. Without it, the volume is empty.
    """
    validate_image(image)
    return {
        "apiVersion": API_VERSION,
        "kind": "Sandbox",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": dict(labels)},
        "spec": {
            "operatingMode": "Running",
            "podTemplate": {
                "metadata": {"labels": dict(labels)},
                "spec": {
                    "serviceAccountName": "hermes-sandbox",
                    "automountServiceAccountToken": False,
                    # Do not add the variables of the other services. The kubelet adds the ones
                    # of the service `kubernetes` anyway, so the address is not secret.
                    "enableServiceLinks": False,
                    "imagePullSecrets": [{"name": "ghcr-pull"}],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": UID,
                        "runAsGroup": UID,
                        "fsGroup": UID,
                        # The volume of a Nix base is large. Change the owner only when the root directory differs.
                        "fsGroupChangePolicy": "OnRootMismatch",
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "affinity": {
                        "nodeAffinity": {
                            "requiredDuringSchedulingIgnoredDuringExecution": {
                                "nodeSelectorTerms": [
                                    {
                                        "matchExpressions": [
                                            {
                                                "key": "kubernetes.io/hostname",
                                                "operator": "NotIn",
                                                "values": ["home-ops-03"],
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    },
                    "initContainers": [
                        {
                            "name": INIT,
                            "image": image,
                            "command": ["/bin/sh", "-c", _COPY_CLOSURE],
                            "securityContext": _security_context(),
                            "volumeMounts": [{"name": VOLUME, "mountPath": "/data"}],
                        }
                    ],
                    "containers": [
                        {
                            "name": SHELL,
                            "image": image,
                            "securityContext": _security_context(),
                            "volumeMounts": [
                                {"name": VOLUME, "mountPath": "/nix", "subPath": "nix"},
                                {"name": VOLUME, "mountPath": "/workspace", "subPath": "workspace"},
                                {"name": VOLUME, "mountPath": "/home/sandbox", "subPath": "home"},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}}],
                },
            },
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": VOLUME},
                    "spec": _volume_spec(nix_base),
                }
            ],
        },
    }

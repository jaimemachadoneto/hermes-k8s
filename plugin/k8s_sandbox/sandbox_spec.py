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
# The home is in the workspace mount, so one volume path keeps the files and the dotfiles.
HOME = "/workspace/.home"
INIT = "copy-closure"
UID = 10000

# The policy pins the image by digest, so a tag that moves cannot change a sandbox.
_DIGEST = re.compile(r".+@sha256:[a-f0-9]{64}")

# The volume at /nix hides the store of the image (ADR 0003). The init step copies the store
# into the volume. The marker `.closure-copied` shows that a copy is complete. If the volume
# has the marker, it is a Nix base or an earlier copy, and the step does nothing. If the
# volume has no marker, the step removes the incomplete copy first. The store is read-only,
# so run `chmod` before `rm`. The workspace and the home (`/workspace/.home`) are in the volume
# too. `mkdir -p` keeps the files that a Nix base has.
# TODO: Make the Job that builds the Nix base write the marker.
_COPY_CLOSURE = """\
set -eu
mkdir -p /data/nix /data/workspace
# An older sandbox kept its home in /data/home. Move it into the workspace once.
if [ -d /data/home ] && [ ! -e /data/workspace/.home ]; then
  cp -a /data/home /data/workspace/.home
fi
mkdir -p /data/workspace/.home
if [ ! -e /data/nix/.closure-copied ]; then
  if [ -e /data/nix/store ]; then
    chmod -R u+w /data/nix/store
    rm -rf /data/nix/store
  fi
  cp -a --no-preserve=ownership /nix/store /data/nix/store
  touch /data/nix/.closure-copied
fi
"""


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


def build_sandbox(name: str, *, image: str, labels: dict[str, str]) -> dict:
    """Return the Sandbox object. The object holds no secret and no token."""
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
                            # The image sets HOME to a read-only path. A plain value passes the policy `hermes-sandbox-pod`
                            # (it refuses only `valueFrom` and `envFrom`).
                            "env": [{"name": "HOME", "value": HOME}],
                            "volumeMounts": [
                                {"name": VOLUME, "mountPath": "/nix", "subPath": "nix"},
                                {"name": VOLUME, "mountPath": "/workspace", "subPath": "workspace"},
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
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "storageClassName": "ceph-block",
                        "resources": {"requests": {"storage": "20Gi"}},
                    },
                }
            ],
        },
    }

"""The one function that builds a Sandbox. The admission policy `hermes-sandbox-pod` of
home-ops accepts only this shape: `tests/test_sandbox_spec.py` holds a copy of its rules.
"""

from __future__ import annotations

import re

NAMESPACE = "hermes-sandbox"
API_VERSION = "agents.x-k8s.io/v1beta1"
SHELL = "shell"
INIT = "copy-closure"
UID = 10000

# The policy pins the image by digest, so a tag that moves cannot change a sandbox.
_DIGEST = re.compile(r"@sha256:[a-f0-9]{64}$")

# The volume at /nix hides the store of the image (ADR 0003), so the init step copies the
# store into the volume. The marker shows that a copy is complete. A volume that has it is a
# Nix base or an earlier copy, and the step does nothing. Without the marker, a half copy is
# removed first. The store is read-only, so it needs `chmod` before `rm`. The workspace and
# the home are in the volume too, and `mkdir -p` keeps what a Nix base has.
_COPY_CLOSURE = """\
set -eu
mkdir -p /data/nix /data/workspace /data/home
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
    if not _DIGEST.search(image):
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
                    # No variable names the API server, so a command does not learn its address.
                    "enableServiceLinks": False,
                    "imagePullSecrets": [{"name": "ghcr-pull"}],
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": UID,
                        "runAsGroup": UID,
                        "fsGroup": UID,
                        # The volume of a Nix base is big: change its owner only when the root differs.
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
                            "volumeMounts": [{"name": "data", "mountPath": "/data"}],
                        }
                    ],
                    "containers": [
                        {
                            "name": SHELL,
                            "image": image,
                            "securityContext": _security_context(),
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/nix", "subPath": "nix"},
                                {"name": "data", "mountPath": "/workspace", "subPath": "workspace"},
                                {"name": "data", "mountPath": "/home/sandbox", "subPath": "home"},
                                {"name": "tmp", "mountPath": "/tmp"},
                            ],
                        }
                    ],
                    "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}}],
                },
            },
            "volumeClaimTemplates": [
                {
                    "metadata": {"name": "data"},
                    "spec": {
                        "accessModes": ["ReadWriteOnce"],
                        "storageClassName": "ceph-block",
                        "resources": {"requests": {"storage": "20Gi"}},
                    },
                }
            ],
        },
    }

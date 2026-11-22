"""Which Nix base a new sandbox starts from, and which Nix bases the cluster keeps.

A Nix base is a VolumeSnapshot with the labels below. The Nix base Job makes it from one
sandbox image (ADR 0003), so the plugin selects it by the digest of that image. All the
functions here are pure: they take the list that the cluster port returns.
"""

from __future__ import annotations

import logging
import re
import time
from collections import defaultdict

from .cluster import Cluster, ClusterError, NixBase

logger = logging.getLogger(__name__)

# The labels that the Job puts on the VolumeSnapshot of a Nix base.
LABEL_KIND = "hermes-k8s/kind"
KIND = "nix-base"
LABEL_DIGEST = "hermes-k8s/image-digest"

# A label value has at most 63 characters, and a sha256 digest has 64. The first 32
# hexadecimal characters are enough to tell two images apart.
_DIGEST_LENGTH = 32
_DIGEST = re.compile(r"@sha256:([a-f0-9]{64})$")

# Each image keeps its two newest Nix bases: the newest one serves new sandboxes, and the
# second one covers a sandbox that starts while a new Nix base is in the making.
KEEP_PER_IMAGE = 2
# A Nix base that is not ready after this time did not finish. Its Job failed.
NOT_READY_SECONDS = 24 * 3600.0


def image_digest_label(image: str) -> str:
    """Return the label value for the digest of an image reference."""
    match = _DIGEST.search(image)
    if match is None:
        raise ValueError(f"the image has no sha256 digest: {image!r}")
    return match.group(1)[:_DIGEST_LENGTH]


def _newest_first(bases: list[NixBase]) -> list[NixBase]:
    return sorted(bases, key=lambda b: b.created, reverse=True)


def select_nix_base(bases: list[NixBase], image: str) -> str | None:
    """Return the name of the newest ready Nix base of the image, or None.

    None means that no Nix base fits, and the volume of the sandbox is empty. A Nix base of
    another image never fits: its store has other paths than the store of this image.
    """
    digest = image_digest_label(image)
    for base in _newest_first(bases):
        if base.ready and base.image_digest == digest:
            return base.name
    return None


def nix_bases_to_delete(
    bases: list[NixBase], current_image: str, *, now: float | None = None
) -> list[str]:
    """Return the names of the Nix bases that the cluster does not need any more.

    Keep the two newest ready Nix bases of each image, and a Nix base that is in the making.
    The newest ready Nix base of the current image is always among the ones to keep.
    """
    now = time.time() if now is None else now
    by_image: dict[str, list[NixBase]] = defaultdict(list)
    for base in bases:
        by_image[base.image_digest].append(base)
    doomed: list[str] = []
    for group in by_image.values():
        ready = _newest_first([b for b in group if b.ready])
        doomed += [b.name for b in ready[KEEP_PER_IMAGE:]]
        doomed += [b.name for b in group if not b.ready and now - b.created > NOT_READY_SECONDS]
    # A rule above must never take the Nix base that a new sandbox of the current image selects.
    selected = select_nix_base(bases, current_image)
    return [name for name in doomed if name != selected]


def choose_nix_base(cluster: Cluster, image: str) -> str | None:
    """Select the Nix base for a new sandbox. A cluster failure gives None: start empty."""
    try:
        return select_nix_base(cluster.list_nix_bases(), image)
    except ClusterError as error:
        logger.warning("Cannot list the Nix bases, the volume starts empty: %s", error)
        return None


def reap_nix_bases(cluster: Cluster, image: str, *, now: float | None = None) -> list[str]:
    """Delete the Nix bases that `nix_bases_to_delete` names. Return the names that went."""
    deleted = []
    for name in nix_bases_to_delete(cluster.list_nix_bases(), image, now=now):
        cluster.delete_nix_base(name)
        deleted.append(name)
    return deleted

"""Seam 1 for the Nix base: the selection by image digest, the fallback and the clean-up."""

import pytest
from images import IMAGE
from k8s_sandbox.cluster import ClusterError, NixBase
from k8s_sandbox.nix_base import (
    image_digest_label,
    nix_bases_to_delete,
    reap_nix_bases,
    select_nix_base,
)
from k8s_sandbox.sandbox_spec import build_sandbox

OTHER = "ghcr.io/jaimemachadoneto/hermes-sandbox:other@sha256:" + "1" * 64
DAY = 86400.0
NOW = 100 * DAY


def base(name, image, age_days, *, ready=True):
    return NixBase(name, image_digest_label(image), NOW - age_days * DAY, ready)


def test_the_digest_label_is_a_valid_label_value_of_the_digest():
    label = image_digest_label(IMAGE)

    assert label == "0" * 32
    assert len(label) <= 63
    assert image_digest_label(OTHER) == "1" * 32
    # The tag does not change the label: the digest is the identity of an image.
    assert image_digest_label(IMAGE.replace(":test", ":other")) == label


def test_the_newest_ready_nix_base_of_the_image_is_selected():
    bases = [
        base("old", IMAGE, 5),
        base("new", IMAGE, 1),
        base("newest-not-ready", IMAGE, 0, ready=False),
        base("other-image", OTHER, 0.5),
    ]

    assert select_nix_base(bases, IMAGE) == "new"
    assert select_nix_base(bases, OTHER) == "other-image"


def test_no_nix_base_for_the_image_gives_the_empty_volume():
    assert select_nix_base([], IMAGE) is None
    assert select_nix_base([base("o", OTHER, 1), base("n", IMAGE, 0, ready=False)], IMAGE) is None


def test_the_clean_up_keeps_the_two_newest_ready_bases_of_each_image():
    bases = [base(f"a{i}", IMAGE, i) for i in range(4)] + [
        base(f"b{i}", OTHER, i) for i in range(3)
    ]

    assert sorted(nix_bases_to_delete(bases, IMAGE, now=NOW)) == ["a2", "a3", "b2"]


def test_the_clean_up_keeps_the_nix_base_of_the_current_image_when_it_is_the_only_one():
    bases = [base("current", IMAGE, 90), base("n0", OTHER, 1), base("n1", OTHER, 2)]

    assert nix_bases_to_delete(bases, IMAGE, now=NOW) == []


def test_the_clean_up_deletes_a_nix_base_that_never_got_ready_only_after_a_day():
    bases = [
        base("building", IMAGE, 0.1, ready=False),
        base("failed", IMAGE, 2, ready=False),
        base("good", IMAGE, 1),
    ]

    assert nix_bases_to_delete(bases, IMAGE, now=NOW) == ["failed"]


def test_the_sandbox_starts_from_the_nix_base_of_its_image(make_env, cluster):
    cluster.nix_bases = [base("old", IMAGE, 3), base("new", IMAGE, 1), base("x", OTHER, 0)]

    env = make_env()

    assert cluster.sandboxes[env.sandbox_name].nix_base == "new"


def test_the_sandbox_starts_from_an_empty_volume_when_no_nix_base_fits(make_env, cluster):
    cluster.nix_bases = [base("x", OTHER, 0)]

    env = make_env()

    assert cluster.sandboxes[env.sandbox_name].nix_base is None


def test_the_sandbox_starts_from_an_empty_volume_when_the_cluster_cannot_list_the_bases(
    make_env, cluster
):
    def fail():
        raise ClusterError("no access")

    cluster.list_nix_bases = fail

    env = make_env()

    assert cluster.sandboxes[env.sandbox_name].nix_base is None


def test_a_resumed_sandbox_keeps_its_volume_and_gets_no_new_nix_base(make_env, cluster):
    first = make_env("task-a")
    cluster.nix_bases = [base("new", IMAGE, 1)]
    first.cleanup()

    make_env("task-a")

    assert cluster.sandboxes[first.sandbox_name].nix_base is None


def test_creating_an_environment_deletes_the_old_bases(make_env, cluster):
    cluster.nix_bases = [base(f"a{i}", IMAGE, i) for i in range(4)]

    make_env()

    assert sorted(b.name for b in cluster.nix_bases) == ["a0", "a1"]


def test_a_failed_clean_up_does_not_stop_the_sandbox(make_env, cluster):
    cluster.nix_bases = [base(f"a{i}", IMAGE, i) for i in range(4)]

    def fail(name):
        raise ClusterError("refused")

    cluster.delete_nix_base = fail

    env = make_env()

    assert cluster.sandboxes[env.sandbox_name].nix_base == "a0"


def test_reap_returns_the_names_that_it_deleted(cluster):
    cluster.nix_bases = [base(f"a{i}", IMAGE, i) for i in range(3)]

    assert reap_nix_bases(cluster, IMAGE, now=NOW) == ["a2"]


def test_the_volume_template_has_the_nix_base_as_data_source():
    sandbox = build_sandbox("hermes-abc", image=IMAGE, labels={}, nix_base="nix-base-1")

    spec = sandbox["spec"]["volumeClaimTemplates"][0]["spec"]
    assert spec["dataSource"] == {
        "apiGroup": "snapshot.storage.k8s.io",
        "kind": "VolumeSnapshot",
        "name": "nix-base-1",
    }
    # A restored volume is as big as the snapshot at least, and the policy allows 20Gi at most.
    assert spec["resources"]["requests"]["storage"] == "20Gi"


def test_a_sandbox_with_a_nix_base_breaks_no_rule_of_the_policy():
    from vap_rules import violations

    sandbox = build_sandbox("hermes-abc", image=IMAGE, labels={}, nix_base="nix-base-1")

    assert violations(sandbox) == []


@pytest.mark.parametrize("name", ["", "A/b", "x" * 300])
def test_a_bad_nix_base_name_is_refused(name):
    with pytest.raises(ValueError, match="Nix base"):
        build_sandbox("hermes-abc", image=IMAGE, labels={}, nix_base=name)

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


def test_the_clean_up_keeps_the_two_newest_ready_bases_of_the_current_image():
    bases = [base(f"a{i}", IMAGE, i) for i in range(4)]

    assert sorted(nix_bases_to_delete(bases, IMAGE, now=NOW)) == ["a2", "a3"]


def test_the_clean_up_deletes_every_base_of_other_images_when_the_current_image_has_a_ready_base():
    bases = [base("a0", IMAGE, 1)] + [base(f"b{i}", OTHER, i + 2) for i in range(3)]
    bases.append(base("c-failed", OTHER, 3, ready=False))
    # A Nix base of another image can be in the making: it has a day before it counts as failed.
    bases.append(base("c-building", OTHER, 0.1, ready=False))

    assert sorted(nix_bases_to_delete(bases, IMAGE, now=NOW)) == ["b0", "b1", "b2", "c-failed"]


def test_the_clean_up_keeps_the_bases_of_other_images_while_the_current_image_has_no_ready_base():
    bases = [base(f"b{i}", OTHER, i + 2) for i in range(4)]
    bases.append(base("a-building", IMAGE, 0.1, ready=False))

    assert nix_bases_to_delete(bases, IMAGE, now=NOW) == []


def test_the_clean_up_keeps_a_not_ready_base_of_another_image_while_the_current_image_has_none():
    bases = [base("b-failed", OTHER, 2, ready=False), base("a-building", IMAGE, 0.1, ready=False)]

    assert nix_bases_to_delete(bases, IMAGE, now=NOW) == []


def test_the_clean_up_never_touches_a_base_without_digest_label():
    bases = [
        base("a0", IMAGE, 1),
        NixBase("no-digest", "", NOW - 50 * DAY, True),
        NixBase("no-digest-building", "", NOW - 50 * DAY, False),
    ]

    assert nix_bases_to_delete(bases, IMAGE, now=NOW) == []


def test_the_clean_up_keeps_a_base_of_another_image_for_an_hour():
    bases = [base("a0", IMAGE, 0.001), base("b0", OTHER, 0.01)]

    assert nix_bases_to_delete(bases, IMAGE, now=NOW) == []


def test_the_clean_up_keeps_a_ready_base_for_an_hour_even_when_it_is_not_among_the_two_newest():
    bases = [base("a0", IMAGE, 0.01), base("a1", IMAGE, 0.02), base("a2", IMAGE, 0.03)]

    assert nix_bases_to_delete(bases, IMAGE, now=NOW) == []


def test_the_clean_up_deletes_the_bases_of_other_images_and_keeps_the_only_old_base_of_the_current_image():
    bases = [base("current", IMAGE, 90), base("n0", OTHER, 1), base("n1", OTHER, 2)]

    assert sorted(nix_bases_to_delete(bases, IMAGE, now=NOW)) == ["n0", "n1"]


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
    calls = []
    original = cluster.create_sandbox
    cluster.create_sandbox = lambda *a, **k: (calls.append(k), original(*a, **k))

    make_env("task-a")

    assert cluster.sandboxes[first.sandbox_name].nix_base is None
    assert calls == []


def test_creating_an_environment_deletes_the_old_bases(make_env, cluster):
    cluster.nix_bases = [base(f"a{i}", IMAGE, i) for i in range(4)]

    make_env()

    assert sorted(b.name for b in cluster.nix_bases) == ["a0", "a1"]


def test_creating_an_environment_deletes_the_bases_of_older_images(make_env, cluster):
    cluster.nix_bases = [base("a0", IMAGE, 1), base("b0", OTHER, 8), base("b1", OTHER, 9)]

    make_env()

    assert [b.name for b in cluster.nix_bases] == ["a0"]


def test_a_failed_clean_up_does_not_stop_the_sandbox(make_env, cluster):
    cluster.nix_bases = [base(f"a{i}", IMAGE, i) for i in range(4)]

    def fail(name):
        raise ClusterError("refused")

    cluster.delete_nix_base = fail

    env = make_env()

    assert cluster.sandboxes[env.sandbox_name].nix_base == "a0"


def test_a_failed_list_during_the_clean_up_does_not_stop_the_sandbox(make_env, cluster):
    calls = []

    def fail():
        calls.append(1)
        raise ClusterError("no access")

    cluster.list_nix_bases = fail

    env = make_env()

    assert len(calls) == 2  # the clean up and the selection
    assert cluster.sandboxes[env.sandbox_name].nix_base is None


def test_the_digest_label_of_an_image_without_digest_is_refused():
    with pytest.raises(ValueError, match="sha256"):
        image_digest_label("ghcr.io/jaimemachadoneto/hermes-sandbox:1")


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


def test_a_nix_base_name_of_253_characters_is_valid_and_of_254_is_not():
    build_sandbox("hermes-abc", image=IMAGE, labels={}, nix_base="a" * 253)
    with pytest.raises(ValueError, match="Nix base"):
        build_sandbox("hermes-abc", image=IMAGE, labels={}, nix_base="a" * 254)


@pytest.mark.parametrize("name", ["", "A/b", "x" * 300])
def test_a_bad_nix_base_name_is_refused(name):
    with pytest.raises(ValueError, match="Nix base"):
        build_sandbox("hermes-abc", image=IMAGE, labels={}, nix_base=name)

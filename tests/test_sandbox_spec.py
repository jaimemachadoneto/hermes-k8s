"""The Sandbox that the plugin makes must pass the admission policy of home-ops."""

import copy
from pathlib import Path

import pytest
import yaml
from images import IMAGE
from k8s_sandbox.sandbox_spec import build_sandbox, validate_image
from vap_rules import violations

GOOD = yaml.safe_load((Path(__file__).parent / "fixtures" / "sandbox-good.yaml").read_text())


def make():
    return build_sandbox("hermes-abc", image=IMAGE, labels={"hermes-task": "hermes-abc"})


def test_the_copy_of_the_rules_accepts_the_fixture_of_home_ops():
    assert violations(GOOD) == []


@pytest.mark.parametrize(
    ("change", "rule"),
    [
        (
            lambda s: s["spec"]["podTemplate"]["spec"].update(serviceAccountName="default"),
            "account",
        ),
        (
            lambda s: s["spec"]["podTemplate"]["spec"].update(automountServiceAccountToken=True),
            "account",
        ),
        (lambda s: s["spec"]["podTemplate"]["spec"].update(hostNetwork=True), "host"),
        (lambda s: s["spec"]["podTemplate"]["spec"].update(tolerations=[{}]), "scheduling"),
        (lambda s: s["spec"]["podTemplate"]["spec"].pop("affinity"), "affinity"),
        (
            lambda s: s["spec"]["podTemplate"]["spec"]["volumes"][0]["emptyDir"].pop("sizeLimit"),
            "volume",
        ),
        (lambda s: s["spec"]["podTemplate"]["spec"]["initContainers"][0].update(name="x"), "init"),
        (
            lambda s: s["spec"]["podTemplate"]["spec"]["initContainers"][0].update(
                image="ghcr.io/jaimemachadoneto/hermes-sandbox:1"
            ),
            "image",
        ),
        (
            lambda s: s["spec"]["podTemplate"]["spec"]["containers"][0].update(
                image="ghcr.io/jaimemachadoneto/hermes-sandbox"
            ),
            "image",
        ),
        (
            lambda s: s["spec"]["podTemplate"]["spec"].update(imagePullSecrets=[{"name": "x"}]),
            "pull-secret",
        ),
        (
            lambda s: s["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"].update(
                storage="50Gi"
            ),
            "template",
        ),
        (
            lambda s: s["spec"]["volumeClaimTemplates"][0]["spec"].update(storageClassName="x"),
            "template",
        ),
    ],
)
def test_the_copy_of_the_rules_refuses_a_bad_sandbox(change, rule):
    bad = copy.deepcopy(GOOD)
    change(bad)

    assert rule in violations(bad)


def test_the_sandbox_of_the_plugin_breaks_no_rule_of_the_policy():
    assert violations(make()) == []


def test_the_sandbox_has_the_shape_of_the_fixture_of_home_ops():
    sandbox, good = make(), GOOD
    pod, good_pod = sandbox["spec"]["podTemplate"]["spec"], good["spec"]["podTemplate"]["spec"]

    assert sandbox["apiVersion"] == good["apiVersion"]
    assert sandbox["kind"] == "Sandbox"
    assert sandbox["metadata"]["namespace"] == good["metadata"]["namespace"]
    assert sandbox["spec"]["operatingMode"] == "Running"
    for key in (
        "serviceAccountName",
        "automountServiceAccountToken",
        "imagePullSecrets",
        "affinity",
    ):
        assert pod[key] == good_pod[key]
    assert pod["volumes"] == good_pod["volumes"]
    template, good_template = (s["spec"]["volumeClaimTemplates"][0] for s in (sandbox, good))
    assert template["metadata"] == good_template["metadata"]
    # With no Nix base, the volume has no data source (it is empty).
    assert template["spec"] == {k: v for k, v in good_template["spec"].items() if k != "dataSource"}
    mounts = {(m["mountPath"], m.get("subPath")) for m in pod["containers"][0]["volumeMounts"]}
    assert {("/nix", "nix"), ("/workspace", "workspace"), ("/tmp", None)} <= mounts


def test_the_init_container_copies_the_closure_of_the_image_into_the_volume():
    pod = make()["spec"]["podTemplate"]["spec"]
    (init,) = pod["initContainers"]

    assert init["name"] == "copy-closure"
    assert init["image"] == pod["containers"][0]["image"] == IMAGE
    assert init["volumeMounts"] == [{"name": "data", "mountPath": "/data"}]
    script = init["command"][-1]
    # The volume at /nix hides the store of the image (ADR 0003), so the store goes to the volume.
    assert "/nix/store" in script
    assert "/data/nix/store" in script
    assert "mkdir -p /data/nix /data/workspace" in script
    # A volume that has the marker is a Nix base or an earlier copy: the init step does nothing.
    assert ".closure-copied" in script


def test_the_pod_has_no_service_account_token_secret_or_service_link():
    sandbox = make()
    pod = sandbox["spec"]["podTemplate"]["spec"]
    text = yaml.safe_dump(sandbox)

    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    assert "secret" not in text.lower().replace("imagepullsecrets", "")
    assert "valueFrom" not in text
    for container in pod["containers"] + pod["initContainers"]:
        context = container["securityContext"]
        assert context["allowPrivilegeEscalation"] is False
        assert context["readOnlyRootFilesystem"] is True
        assert context["capabilities"] == {"drop": ["ALL"]}
    assert pod["securityContext"]["runAsUser"] == 10000
    assert pod["securityContext"]["runAsNonRoot"] is True


def test_the_labels_go_to_the_sandbox_and_to_the_pod():
    sandbox = make()

    assert sandbox["metadata"]["labels"] == {"hermes-task": "hermes-abc"}
    assert sandbox["spec"]["podTemplate"]["metadata"]["labels"] == {"hermes-task": "hermes-abc"}


@pytest.mark.parametrize(
    "image",
    [
        "ghcr.io/jaimemachadoneto/hermes-sandbox",
        "ghcr.io/jaimemachadoneto/hermes-sandbox:1.0.0",
        "ghcr.io/jaimemachadoneto/hermes-sandbox@sha256:abc",
        "ghcr.io/jaimemachadoneto/hermes-sandbox@sha512:" + "0" * 64,
        "",
    ],
)
def test_an_image_without_a_sha256_digest_is_refused(image):
    with pytest.raises(ValueError, match="digest"):
        validate_image(image)
    with pytest.raises(ValueError, match="digest"):
        build_sandbox("x", image=image, labels={})


def test_an_image_with_a_digest_is_accepted_with_and_without_a_tag():
    validate_image("ghcr.io/jaimemachadoneto/hermes-sandbox@sha256:" + "a" * 64)
    validate_image(IMAGE)

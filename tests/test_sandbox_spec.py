"""The Sandbox that the plugin makes must pass the admission policy of home-ops."""

import copy
import os
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
        (
            lambda s: s["spec"]["podTemplate"]["spec"]["volumes"][1]["secret"].update(
                secretName="ghcr-pull"
            ),
            "volume",
        ),
        (
            lambda s: s["spec"]["podTemplate"]["spec"]["volumes"][1]["secret"].update(
                secretName="hermes-sandbox-"
            ),
            "volume",
        ),
        (
            lambda s: s["spec"]["podTemplate"]["spec"]["volumes"].append(
                {"name": "h", "hostPath": {"path": "/"}}
            ),
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
    assert {
        ("/nix", "nix"),
        ("/workspace", "workspace"),
        ("/tmp", None),
        ("/run/secrets/github", None),
    } <= mounts
    secret_mount = next(
        m for m in pod["containers"][0]["volumeMounts"] if m["mountPath"] == "/run/secrets/github"
    )
    assert secret_mount["readOnly"] is True


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


def _run_init_script(tmp_path, *, marker, half_copy, load_db_fails=False):
    """Run the copy script with the paths /data and /nix/store moved into `tmp_path`."""
    import subprocess

    image_store, data = tmp_path / "image-store", tmp_path / "data"
    (image_store / "pkg" / "bin").mkdir(parents=True)
    (image_store / "pkg" / "bin" / "tool").write_text("tool")
    (image_store / "pkg" / "bin").chmod(0o555)  # a read-only store, as in the image
    if marker or half_copy:
        (data / "nix" / "store" / "stale" / "sub").mkdir(parents=True)
        (data / "nix" / "store" / "stale" / "sub" / "f").write_text("x")
        (data / "nix" / "store" / "stale" / "sub").chmod(0o555)
    if marker:
        (data / "nix" / ".closure-copied").write_text("")
    script = make()["spec"]["podTemplate"]["spec"]["initContainers"][0]["command"][-1]
    # Move the paths into `tmp_path`. A placeholder stops one replacement from changing the result of the next.
    script = script.replace("/data/nix/store", "@DST@").replace("/nix/store", "@SRC@")
    script = script.replace("/data", "@DATA@").replace("/etc/nix/registration", "@REG@")
    # A stand-in for nix-store: it records the state directory and the input that it gets.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "nix-store").write_text(
        f'#!/bin/sh\necho "$NIX_STATE_DIR $*" > {tmp_path}/load-db.args\ncat > {tmp_path}/load-db.input\n'
        f'[ -e "{tmp_path}/data/nix/.closure-copied" ] && touch {tmp_path}/marker-early\n'
        f'exit "${{STUB_EXIT:-0}}"\n'
    )
    (bin_dir / "nix-store").chmod(0o755)
    (tmp_path / "registration").write_text("registration of the image\n")
    for placeholder, path in (
        ("@REG@", str(tmp_path / "registration")),
        ("@DST@", f"{data}/nix/store"),
        ("@SRC@", str(image_store)),
        ("@DATA@", str(data)),
    ):
        script = script.replace(placeholder, path)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    if load_db_fails:
        env["STUB_EXIT"] = "1"
    subprocess.run(["sh", "-c", script], check=True, env=env)
    return data


def test_the_init_step_copies_the_store_into_an_empty_volume(tmp_path):
    data = _run_init_script(tmp_path, marker=False, half_copy=False)

    assert (data / "nix" / "store" / "pkg" / "bin" / "tool").read_text() == "tool"
    assert (data / "nix" / ".closure-copied").exists()
    assert (data / "workspace" / ".home").is_dir()


def test_the_init_step_registers_the_paths_of_the_image_in_the_database_of_the_volume(tmp_path):
    data = _run_init_script(tmp_path, marker=False, half_copy=False)

    assert (tmp_path / "load-db.args").read_text() == f"{data}/nix/var/nix --load-db\n"
    assert (tmp_path / "load-db.input").read_text() == "registration of the image\n"


def test_the_init_step_writes_the_marker_only_after_the_database_is_loaded(tmp_path):
    _run_init_script(tmp_path, marker=False, half_copy=False)

    assert not (tmp_path / "marker-early").exists()


def test_a_failed_load_of_the_database_leaves_no_marker(tmp_path):
    import subprocess

    with pytest.raises(subprocess.CalledProcessError):
        _run_init_script(tmp_path, marker=False, half_copy=False, load_db_fails=True)

    assert not (tmp_path / "data" / "nix" / ".closure-copied").exists()


def test_the_init_step_replaces_an_incomplete_copy(tmp_path):
    data = _run_init_script(tmp_path, marker=False, half_copy=True)

    assert not (data / "nix" / "store" / "stale").exists()
    assert (data / "nix" / "store" / "pkg" / "bin" / "tool").exists()
    assert (data / "nix" / ".closure-copied").exists()


def test_the_init_step_does_nothing_when_the_volume_has_the_marker(tmp_path):
    data = _run_init_script(tmp_path, marker=True, half_copy=False)

    assert (data / "nix" / "store" / "stale" / "sub" / "f").exists()
    assert not (data / "nix" / "store" / "pkg").exists()
    assert not (tmp_path / "load-db.args").exists()


def test_the_pod_has_no_service_account_token_secret_or_service_link():
    sandbox = make()
    pod = sandbox["spec"]["podTemplate"]["spec"]
    text = yaml.safe_dump(sandbox)

    assert pod["automountServiceAccountToken"] is False
    assert pod["enableServiceLinks"] is False
    # The one Secret is the one of the profile, mounted as files and never as variables.
    assert [v["name"] for v in pod["volumes"]] == ["tmp", "github"]
    assert [k for v in pod["volumes"] for k in v if k != "name"] == ["emptyDir", "secret"]
    assert "secretKeyRef" not in text and "secretRef" not in text
    # A plain value such as HOME is allowed. A value from a Secret is not.
    for container in pod["containers"] + pod["initContainers"]:
        assert "envFrom" not in container
        for variable in container.get("env", []):
            assert set(variable) == {"name", "value"}, variable
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
        "ghcr.io/jaimemachadoneto/hermes-sandbox@sha256:" + "0" * 64 + "\n",
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


def test_the_home_is_in_the_workspace_volume():
    container = make()["spec"]["podTemplate"]["spec"]["containers"][0]

    assert {"name": "HOME", "value": "/workspace/.home"} in container["env"]
    # The home is inside /workspace, so one mount keeps both through a suspend.
    assert "/home/sandbox" not in {m["mountPath"] for m in container["volumeMounts"]}


def test_the_tmp_is_an_empty_dir():
    pod = make()["spec"]["podTemplate"]["spec"]
    container = pod["containers"][0]

    assert {"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}} in pod["volumes"]
    assert {"name": "tmp", "mountPath": "/tmp"} in container["volumeMounts"]


def test_the_init_step_moves_an_older_home_into_the_workspace(tmp_path):
    (tmp_path / "data" / "home").mkdir(parents=True)
    (tmp_path / "data" / "home" / ".bashrc").write_text("old")

    data = _run_init_script(tmp_path, marker=True, half_copy=False)

    assert (data / "workspace" / ".home" / ".bashrc").read_text() == "old"

"""Tests of the release tooling: the scripts, the workflows and the Renovate configuration."""

import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import vap_rules
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def run(args, **kwargs):
    return subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)


# --- set-image-refs.py ---------------------------------------------------------------------


def set_refs(tmp_path, hermes_tag, sandbox_ref):
    target = tmp_path / "helmrelease.yaml"
    target.write_text((FIXTURES / "helmrelease-excerpt.yaml").read_text())
    result = run(
        [
            sys.executable,
            "-I",
            str(ROOT / "scripts" / "set-image-refs.py"),
            str(target),
            "--hermes-tag",
            hermes_tag,
            "--sandbox-ref",
            sandbox_ref,
        ]
    )
    return result, target


SANDBOX = "ghcr.io/jaimemachadoneto/hermes-sandbox:sha-" + "1" * 40 + "@" + DIGEST


def test_set_image_refs_changes_only_the_two_lines(tmp_path):
    result, target = set_refs(tmp_path, f"sha-{'1' * 40}@{OTHER}", SANDBOX)

    assert result.returncode == 0, result.stderr
    before = (FIXTURES / "helmrelease-excerpt.yaml").read_text().splitlines()
    after = target.read_text().splitlines()
    changed = [(b, a) for b, a in zip(before, after, strict=True) if b != a]
    assert len(before) == len(after)
    assert len(changed) == 2
    assert f"tag: sha-{'1' * 40}@{OTHER}" in after[9]
    assert after[13].strip() == f"HERMES_K8S_SANDBOX_IMAGE: {SANDBOX}"


def test_set_image_refs_refuses_a_ref_with_no_digest(tmp_path):
    result, target = set_refs(tmp_path, "sha-abc", SANDBOX)

    assert result.returncode != 0
    assert target.read_text() == (FIXTURES / "helmrelease-excerpt.yaml").read_text()


def test_set_image_refs_refuses_a_sandbox_ref_of_another_image(tmp_path):
    result, _ = set_refs(tmp_path, f"sha-abc@{OTHER}", f"ghcr.io/evil/image:x@{DIGEST}")

    assert result.returncode != 0


def test_set_image_refs_fails_when_the_file_has_no_hermes_image(tmp_path):
    target = tmp_path / "helmrelease.yaml"
    target.write_text("spec: {}\n")

    result = run(
        [
            sys.executable,
            "-I",
            str(ROOT / "scripts" / "set-image-refs.py"),
            str(target),
            "--hermes-tag",
            f"sha-abc@{OTHER}",
            "--sandbox-ref",
            SANDBOX,
        ]
    )

    assert result.returncode != 0


def set_nix_base(tmp_path, sandbox_ref, text=None):
    helm = tmp_path / "helmrelease.yaml"
    helm.write_text((FIXTURES / "helmrelease-excerpt.yaml").read_text())
    nix_base = tmp_path / "nix-base.yaml"
    nix_base.write_text((FIXTURES / "nix-base-excerpt.yaml").read_text() if text is None else text)
    result = run(
        [
            sys.executable,
            "-I",
            str(ROOT / "scripts" / "set-image-refs.py"),
            str(helm),
            "--hermes-tag",
            f"sha-abc@{OTHER}",
            "--sandbox-ref",
            sandbox_ref,
            "--nix-base-file",
            str(nix_base),
        ]
    )
    return result, helm, nix_base


def test_set_image_refs_pins_the_three_sandbox_containers_of_the_nix_base(tmp_path):
    result, _, nix_base = set_nix_base(tmp_path, SANDBOX)

    assert result.returncode == 0, result.stderr
    before = (FIXTURES / "nix-base-excerpt.yaml").read_text().splitlines()
    after = nix_base.read_text().splitlines()
    changed = [a for b, a in zip(before, after, strict=True) if b != a]
    assert len(before) == len(after)
    assert changed == [f"              image: {SANDBOX}"] * 3
    assert "d4a029274fa133e91f94fc56d20428c71c701285" not in nix_base.read_text()


def test_set_image_refs_keeps_the_snapshot_image_of_the_nix_base(tmp_path):
    result, _, nix_base = set_nix_base(tmp_path, SANDBOX)

    assert result.returncode == 0, result.stderr
    snapshot = "image: alpine/k8s:1.37.2@sha256:7a82aeefea3d554a43a8676b137f2b88bae6c1fe11c13862b191b3a086cc28c0"
    assert snapshot in nix_base.read_text()


def test_set_image_refs_gives_the_helmrelease_and_the_nix_base_the_same_reference(tmp_path):
    result, helm, nix_base = set_nix_base(tmp_path, SANDBOX)

    assert result.returncode == 0, result.stderr
    assert f"HERMES_K8S_SANDBOX_IMAGE: {SANDBOX}" in helm.read_text()


def test_set_image_refs_replaces_a_sandbox_line_with_no_digest_and_keeps_its_comment(tmp_path):
    old = "- image: ghcr.io/jaimemachadoneto/hermes-sandbox:latest # old\n"
    result, _, nix_base = set_nix_base(tmp_path, SANDBOX, text=old)

    assert result.returncode == 0, result.stderr
    assert nix_base.read_text() == f"- image: {SANDBOX} # old\n"


def test_set_image_refs_pins_a_nix_base_image_that_has_no_tag(tmp_path):
    old = "- image: ghcr.io/jaimemachadoneto/hermes-sandbox@sha256:" + "7" * 64 + "\n"
    result, _, nix_base = set_nix_base(tmp_path, SANDBOX, text=old)

    assert result.returncode == 0, result.stderr
    assert nix_base.read_text() == f"- image: {SANDBOX}\n"


def test_set_image_refs_fails_when_a_sandbox_line_is_in_a_form_that_it_cannot_replace(tmp_path):
    text = (FIXTURES / "nix-base-excerpt.yaml").read_text()
    text += (
        '            - image: "ghcr.io/jaimemachadoneto/hermes-sandbox:sha-1@sha256:'
        + "7" * 64
        + '"\n'
    )
    result, helm, nix_base = set_nix_base(tmp_path, SANDBOX, text=text)

    assert result.returncode != 0
    assert "not replaced" in result.stderr
    assert nix_base.read_text() == text
    assert helm.read_text() == (FIXTURES / "helmrelease-excerpt.yaml").read_text()


def test_set_image_refs_fails_when_the_nix_base_has_no_sandbox_image(tmp_path):
    only_alpine = "image: alpine/k8s:1.37.2@sha256:" + "7" * 64 + "\n"
    result, helm, nix_base = set_nix_base(tmp_path, SANDBOX, text=only_alpine)

    assert result.returncode != 0
    assert "sandbox image" in result.stderr
    assert nix_base.read_text() == only_alpine
    # The script changes no file when one file fails. The PR therefore never has only a part of the change.
    assert helm.read_text() == (FIXTURES / "helmrelease-excerpt.yaml").read_text()


def test_the_sandbox_reference_of_the_script_matches_the_vap_pattern(tmp_path):
    result, target = set_refs(tmp_path, f"sha-abc@{OTHER}", SANDBOX)

    assert result.returncode == 0
    line = next(x for x in target.read_text().splitlines() if "HERMES_K8S_SANDBOX_IMAGE" in x)
    assert vap_rules.IMAGE_PATTERN.fullmatch(line.split(": ", 1)[1])


# --- publish-image.sh ----------------------------------------------------------------------


@pytest.fixture
def stubs(tmp_path):
    """A `nix` and a `skopeo` that record their calls. STUB_FAIL names the skopeo verb to fail."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    (tmp_path / "image.tar").write_text("tar")
    nix = bin_dir / "nix"
    nix.write_text(f'#!/usr/bin/env bash\necho "nix $*" >> {log}\necho {tmp_path}/image.tar\n')
    skopeo = bin_dir / "skopeo"
    skopeo.write_text(
        f"""#!/usr/bin/env bash
echo "skopeo $*" >> {log}
verb=
while [ $# -gt 0 ]; do
  case "$1" in
    login|copy) verb=$1 ;;
    --authfile) auth=$2; shift ;;
    --digestfile) digestfile=$2; shift ;;
  esac
  shift
done
[ "${{STUB_FAIL:-}}" = "$verb" ] && {{ echo "boom" >&2; exit 1; }}
if [ "$verb" = login ]; then cat > "$auth"; fi
if [ -n "${{digestfile:-}}" ]; then printf '%s' "{DIGEST}" > "$digestfile"; fi
exit 0
"""
    )
    for tool in (nix, skopeo):
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "TMPDIR": str(tmp),
        "REGISTRY_USER": "jaime",
        "REGISTRY_TOKEN": "s3cret-token",
    }
    return env, log, tmp


def publish(env, *args):
    return run([str(ROOT / "scripts" / "publish-image.sh"), *args], env=env)


def test_publish_prints_the_reference_with_the_digest(stubs):
    env, log, _ = stubs

    result = publish(env, "hermes-image", "ghcr.io/o/hermes", "sha-1", "1.2.3")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ghcr.io/o/hermes:sha-1@{DIGEST}"
    calls = log.read_text()
    assert "docker-archive:" in calls
    assert f"docker://ghcr.io/o/hermes@{DIGEST} docker://ghcr.io/o/hermes:1.2.3" in calls


@pytest.mark.parametrize("failing", ["login", "copy"])
def test_publish_fails_when_skopeo_fails(stubs, failing):
    env, _, _ = stubs

    result = publish({**env, "STUB_FAIL": failing}, "hermes-image", "ghcr.io/o/hermes", "sha-1")

    assert result.returncode != 0
    assert result.stdout == ""


def test_publish_leaves_no_login_on_disk(stubs):
    env, _, tmp = stubs

    publish(env, "hermes-image", "ghcr.io/o/hermes", "sha-1")
    publish({**env, "STUB_FAIL": "copy"}, "hermes-image", "ghcr.io/o/hermes", "sha-1")

    assert list(tmp.iterdir()) == []


def test_publish_does_not_put_the_token_in_an_argument(stubs):
    env, log, _ = stubs

    publish(env, "hermes-image", "ghcr.io/o/hermes", "sha-1")

    assert "s3cret-token" not in log.read_text()


def test_publish_needs_a_tag(stubs):
    env, _, _ = stubs

    assert publish(env, "hermes-image", "ghcr.io/o/hermes").returncode != 0


def test_both_push_verbs_use_the_publish_script():
    devenv = (ROOT / "devenv.nix").read_text()

    for verb in ("push-image", "push-hermes-image"):
        body = devenv.split(f"base.commands.{verb} =", 1)[1].split("];", 1)[0]
        assert "scripts/publish-image.sh" in body
        assert "| sed" not in body
    assert "base.release.enable = true;" in devenv


# --- check-vap-drift.py --------------------------------------------------------------------


def drift(vap):
    return run([sys.executable, "-I", str(ROOT / "scripts" / "check-vap-drift.py"), str(vap)])


def test_vap_drift_accepts_the_same_pattern():
    assert drift(FIXTURES / "vap-excerpt.yaml").returncode == 0


def test_vap_drift_finds_a_changed_pattern(tmp_path):
    changed = tmp_path / "vap.yaml"
    text = (FIXTURES / "vap-excerpt.yaml").read_text()
    changed.write_text(text.replace("hermes-sandbox", "hermes-sandbox2"))

    result = drift(changed)

    assert result.returncode != 0
    assert "imagePattern" in result.stderr


def test_vap_drift_fails_when_the_policy_has_no_pattern(tmp_path):
    empty = tmp_path / "vap.yaml"
    empty.write_text("spec: {}\n")

    assert drift(empty).returncode != 0


# --- workflows -----------------------------------------------------------------------------


def workflow(name):
    data = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())
    data["on"] = data.pop(True, data.get("on"))  # YAML reads the key `on` as True
    return data


def steps_text(wf):
    return json.dumps(wf)


def test_release_runs_on_a_version_tag_or_the_button():
    wf = workflow("release.yaml")

    assert wf["on"]["push"] == {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}
    assert set(wf["on"]) == {"push", "workflow_dispatch"}


def test_release_jobs_run_on_hosted_runners_with_least_privilege():
    wf = workflow("release.yaml")

    assert wf["permissions"] == {"contents": "read"}
    promote = wf["jobs"]["promote"]
    assert promote["runs-on"] == "ubuntu-latest"
    # The job reads the CI runs and copies a tag inside ghcr.io. It signs nothing.
    assert promote["permissions"] == {"contents": "read", "actions": "read", "packages": "write"}
    assert wf["jobs"]["gitops"]["runs-on"] == "ubuntu-latest"
    assert wf["jobs"]["gitops"]["needs"] == "promote"


def test_release_does_not_build_or_install_nix():
    text = (ROOT / ".github" / "workflows" / "release.yaml").read_text()

    # The file name nix-base.yaml and the variable NIX_BASE_FILE are not an install of Nix.
    text = re.sub(r"nix-base\.yaml|NIX_BASE_FILE", "", text)
    assert not re.search(r"\bnix\b|cachix|determinate", text, re.IGNORECASE)
    assert "trivy" not in text
    assert "publish-image.sh" not in text


def test_release_waits_for_the_ci_run_of_the_commit_on_main():
    wf = workflow("release.yaml")
    steps = wf["jobs"]["promote"]["steps"]
    wait = next(s for s in steps if "wait-for-ci.sh" in s.get("run", ""))

    assert 'scripts/wait-for-ci.sh "$RELEASE_SHA"' in wait["run"]
    assert wait["env"]["GH_TOKEN"] == "${{ secrets.GITHUB_TOKEN }}"
    # The wait (45 min at most) must fit in the time limit of the job.
    assert wf["jobs"]["promote"]["timeout-minutes"] >= 60
    # The wait comes before the copy of the images.
    copy = next(i for i, s in enumerate(steps) if "promote-image.sh" in s.get("run", ""))
    assert steps.index(wait) < copy


def test_release_copies_the_ci_images_with_the_promote_script():
    wf = workflow("release.yaml")
    step = next(s for s in wf["jobs"]["promote"]["steps"] if "promote-image.sh" in s.get("run", ""))

    assert '"sha-$RELEASE_SHA"' in step["run"]
    assert "$VERSION" in step["run"] or "$version" in step["run"]
    for image in ("SANDBOX_IMAGE", "HERMES_IMAGE"):
        assert image in step["run"]
    assert wf["jobs"]["promote"]["outputs"].keys() == {"hermes-tag", "sandbox-ref", "version"}


def test_release_opens_the_pull_request_with_the_secret_of_the_other_repositories():
    text = (ROOT / ".github" / "workflows" / "release.yaml").read_text()

    assert "secrets.MY_PAT" in text
    assert "repository: jaimemachadoneto/home-ops" in text
    assert "kubernetes/apps/ai/hermes/app/helmrelease.yaml" in text
    assert "scripts/set-image-refs.py" in text


def test_the_gitops_job_pins_the_nix_base_in_the_same_pull_request():
    wf = workflow("release.yaml")
    text = (ROOT / ".github" / "workflows" / "release.yaml").read_text()
    step = next(s for s in wf["jobs"]["gitops"]["steps"] if "set-image-refs" in s.get("run", ""))
    pr = next(
        s for s in wf["jobs"]["gitops"]["steps"] if "create-pull-request" in s.get("uses", "")
    )

    assert (
        wf["env"]["NIX_BASE_FILE"]
        == "kubernetes/apps/hermes-sandbox/sandbox-tenant/app/nix-base.yaml"
    )
    assert '--nix-base-file "gitops/$NIX_BASE_FILE"' in step["run"]
    assert "env.GITOPS_FILE" in pr["with"]["body"]
    assert "env.NIX_BASE_FILE" in pr["with"]["body"]


def test_ci_runs_for_each_pull_request_on_a_hosted_runner():
    wf = workflow("ci.yaml")

    job = wf["jobs"]["check"]
    assert "pull_request" in wf["on"]
    assert "pull_request_target" not in wf["on"]
    assert job["runs-on"] == "ubuntu-latest"
    # A fork pull request runs after the approval that the repository setting requires.
    assert "if" not in job
    text = (ROOT / ".github" / "workflows" / "ci.yaml").read_text()
    assert "nix flake check" in text
    assert "pytest" in text


def test_no_job_runs_on_the_self_hosted_runner():
    for path in (ROOT / ".github" / "workflows").glob("*.yaml"):
        for name, job in workflow(path.name)["jobs"].items():
            assert job["runs-on"] == "ubuntu-latest", (path.name, name)
    for path in (ROOT / ".github").rglob("*"):
        if path.is_file() and path.suffix in (".yaml", ".yml", ".nix"):
            assert "arc-jmn-nix" not in path.read_text(), path


def cachix_steps(wf):
    return [
        (name, step)
        for name, job in wf["jobs"].items()
        for step in job["steps"]
        if step.get("uses", "").startswith("cachix/cachix-action@")
    ]


def test_each_build_job_reads_the_public_cache_and_never_pushes_for_a_pull_request():
    steps = cachix_steps(workflow("ci.yaml"))
    assert len(steps) == 2
    for _, step in steps:
        assert step["with"]["name"] == "hermes-k8s"
        assert step["with"]["authToken"] == "${{ secrets.CACHIX_AUTH_TOKEN }}"


def test_the_cache_push_is_skipped_only_for_the_pull_request_event():
    wf = workflow("ci.yaml")
    for _, step in cachix_steps(wf):
        assert step["with"]["skipPush"] == "${{ github.event_name == 'pull_request' }}"
    assert "pull_request" in wf["on"]
    assert wf["on"]["push"] == {"branches": ["main"]}
    release = workflow("release.yaml")
    assert not cachix_steps(release)
    assert "pull_request" not in release["on"]


def test_the_cache_filter_keeps_claude_code_and_the_images_out_of_the_cache():
    steps = cachix_steps(workflow("ci.yaml"))
    assert steps
    for _, step in steps:
        pattern = re.compile(step["with"]["pushFilter"])
        h = "a" * 32
        for skipped in (
            f"/nix/store/{h}-claude-code-2.1.138",
            f"/nix/store/{h}-claude-code-2.1.138-npm-deps",
            f"/nix/store/{h}-hermes.tar.gz",
            f"/nix/store/{h}-hermes-sandbox.tar.gz",
            f"/nix/store/{h}-claude.zst",
            f"/nix/store/{h}-stream-hermes",
            f"/nix/store/{h}-hermes-customisation-layer",
            f"/nix/store/{h}-hermes-conf.json",
            f"/nix/store/{h}-layers.json",
            f"/nix/store/{h}-excludePaths",
            f"/nix/store/{h}-stream-ghcr.io-jaimemachadoneto-hermes",
            f"/nix/store/{h}-stream-ghcr.io-jaimemachadoneto-hermes-sandbox",
            f"/nix/store/{h}-ghcr.io-jaimemachadoneto-hermes-customisation-layer",
            f"/nix/store/{h}-ghcr.io-jaimemachadoneto-hermes-sandbox-customisation-layer",
            f"/nix/store/{h}-ghcr.io-jaimemachadoneto-hermes-layers.json",
            f"/nix/store/{h}-ghcr.io-jaimemachadoneto-hermes-sandbox-conf.json",
        ):
            assert pattern.search(skipped), skipped
        for pushed in (
            f"/nix/store/{h}-python3-3.14.10-env",
            f"/nix/store/{h}-upstream-tools-1.0",
            f"/nix/store/{h}-glibc-2.42",
            f"/nix/store/{h}-libstream-1.0",
            f"/nix/store/{h}-foo-layers.json-bar",
        ):
            assert not pattern.search(pushed), pushed


def test_no_job_spends_time_to_free_disk():
    # A hosted runner had 106 GB free in the first run, and a build needs a few GB.
    for name in ("ci.yaml", "release.yaml"):
        for job_name, job in workflow(name)["jobs"].items():
            for step in job["steps"]:
                assert "/usr/share/dotnet" not in step.get("run", ""), (name, job_name)


def test_actions_are_pinned_by_commit():
    for path in (ROOT / ".github" / "workflows").glob("*.yaml"):
        for use in re.findall(r"uses:\s*(\S+)", path.read_text()):
            assert use.startswith("./") or re.search(r"@[0-9a-f]{40}$", use), (path.name, use)


# --- Renovate ------------------------------------------------------------------------------


def test_renovate_never_proposes_a_calendar_tag_of_hermes():
    config = json.loads((ROOT / "renovate.json").read_text())

    rules = [
        r for r in config["packageRules"] if "NousResearch/hermes-agent" in r["matchPackageNames"]
    ]
    assert len(rules) == 1
    allowed = re.compile(rules[0]["allowedVersions"].strip("/"))
    assert allowed.fullmatch("v0.23.4")
    assert not allowed.fullmatch("v2026.9.24")
    assert config["nix"]["enabled"] is True


def test_the_scan_reads_the_ignore_file_and_scans_each_image_before_it_fails():
    step = next(
        s for s in workflow("ci.yaml")["jobs"]["publish"]["steps"] if "trivy" in s.get("run", "")
    )
    script = step["run"]

    assert "--severity HIGH,CRITICAL --ignore-unfixed" in script
    assert "--ignorefile .trivyignore.yaml" in script
    # A failure in the first image must not hide the findings of the second image.
    assert "--exit-code 1 ||" in script
    assert script.rstrip().endswith('exit "$failed"')
    for image in ("sandbox-image", "hermes-image"):
        assert image in script


# --- CI: the push of the images ------------------------------------------------------------


def test_the_push_job_never_runs_for_a_pull_request():
    wf = workflow("ci.yaml")
    job = wf["jobs"]["publish"]

    assert job["if"] == "github.event_name == 'push'"
    assert job["needs"] == "check"


def test_only_the_push_job_can_write_packages_and_sign():
    wf = workflow("ci.yaml")

    assert wf["permissions"] == {"contents": "read"}
    assert "permissions" not in wf["jobs"]["check"]
    assert wf["jobs"]["publish"]["permissions"] == {
        "contents": "read",
        "packages": "write",
        "id-token": "write",
        "attestations": "write",
    }


def test_the_push_job_scans_then_pushes_then_attests_each_image():
    steps = workflow("ci.yaml")["jobs"]["publish"]["steps"]
    texts = [json.dumps(s) for s in steps]
    scan = next(i for i, t in enumerate(texts) if "trivy" in t)
    push = next(i for i, t in enumerate(texts) if "scripts/publish-image.sh" in t)
    attest = [i for i, s in enumerate(steps) if "attest-build-provenance" in s.get("uses", "")]

    assert scan < push < min(attest)
    # One attestation for each image, stored in the registry.
    assert len(attest) == 2
    for i in attest:
        with_ = steps[i]["with"]
        assert with_["push-to-registry"] is True
        assert with_["subject-digest"].startswith("${{ steps.")
    names = {steps[i]["with"]["subject-name"] for i in attest}
    assert names == {
        "${{ env.SANDBOX_IMAGE }}",
        "${{ env.HERMES_IMAGE }}",
    }


def test_the_push_job_pushes_only_the_commit_tag():
    steps = workflow("ci.yaml")["jobs"]["publish"]["steps"]
    step = next(s for s in steps if "scripts/publish-image.sh" in s.get("run", ""))

    assert '"sha-$COMMIT"' in step["run"]
    assert "$VERSION" not in step["run"]
    assert step["env"]["COMMIT"] == "${{ github.sha }}"


def test_the_images_are_pushed_with_no_secret_beyond_the_token_of_the_run():
    text = (ROOT / ".github" / "workflows" / "ci.yaml").read_text()

    assert "secrets.MY_PAT" not in text


def test_each_ignored_finding_has_a_scope_a_reason_and_an_expiry():
    import datetime

    data = yaml.safe_load((ROOT / ".trivyignore.yaml").read_text())
    # The scan has two scanners: one for vulnerabilities and one for secrets.
    entries = data["vulnerabilities"] + data["secrets"]

    assert data["vulnerabilities"] and data["secrets"]
    today = datetime.date.today()
    for entry in entries:
        assert set(entry) == {"id", "paths", "statement", "expired_at"}, entry
        # A path limits the entry to one package, so the same ID in another package still fails.
        # A secret finding names its path with a leading "/", a vulnerability finding does not.
        assert entry["paths"] and all(
            p.removeprefix("/").startswith("nix/store/*-") for p in entry["paths"]
        )
        assert len(entry["statement"]) > 40
        expiry = entry["expired_at"]
        expiry = (
            expiry if isinstance(expiry, datetime.date) else datetime.date.fromisoformat(expiry)
        )
        assert expiry - today <= datetime.timedelta(days=90)


def test_ci_cancels_only_an_old_run_of_the_same_pull_request():
    # Each runner has its own clone of the Nix store, so runs do not need one shared lock.
    wf = workflow("ci.yaml")

    assert wf["concurrency"] == {
        "group": "ci-${{ github.event.pull_request.number || github.run_id }}",
        "cancel-in-progress": True,
    }


def test_two_releases_never_run_at_the_same_time():
    assert workflow("release.yaml")["concurrency"] == {
        "group": "release",
        "cancel-in-progress": False,
    }


def test_release_checks_the_branch_and_the_ci_run_before_it_copies():
    steps = workflow("release.yaml")["jobs"]["promote"]["steps"]
    checkout = steps[0]
    branch = next(s for s in steps if "merge-base --is-ancestor" in s.get("run", ""))
    ci = next(s for s in steps if "wait-for-ci.sh" in s.get("run", ""))
    copy = next(s for s in steps if "promote-image.sh" in s.get("run", ""))

    assert checkout["with"]["fetch-depth"] == 0
    assert steps.index(branch) < steps.index(ci) < steps.index(copy)
    for step in (branch, ci):
        assert "continue-on-error" not in step
        assert "if" not in step


def test_each_attestation_uses_the_digest_of_the_push_step():
    steps = workflow("ci.yaml")["jobs"]["publish"]["steps"]
    push = next(s for s in steps if "scripts/publish-image.sh" in s.get("run", ""))
    attest = {
        s["with"]["subject-name"]: s
        for s in steps
        if "attest-build-provenance" in s.get("uses", "")
    }

    assert push["id"] == "push"
    assert attest["${{ env.SANDBOX_IMAGE }}"]["with"]["subject-digest"] == (
        "${{ steps.push.outputs.sandbox-digest }}"
    )
    assert attest["${{ env.HERMES_IMAGE }}"]["with"]["subject-digest"] == (
        "${{ steps.push.outputs.hermes-digest }}"
    )
    for step in attest.values():
        assert "if" not in step
        assert "continue-on-error" not in step


def test_no_other_job_of_ci_can_write_packages_or_sign():
    wf = workflow("ci.yaml")

    for name, job in wf["jobs"].items():
        if name != "publish":
            assert not set(job.get("permissions", {})) & {"packages", "id-token", "attestations"}


def test_the_job_logs_in_to_the_registry_before_the_attestations():
    steps = workflow("ci.yaml")["jobs"]["publish"]["steps"]
    login = next(i for i, s in enumerate(steps) if "docker login ghcr.io" in s.get("run", ""))
    first = next(i for i, s in enumerate(steps) if "attest-build-provenance" in s.get("uses", ""))

    assert login < first
    assert "--password-stdin" in steps[login]["run"]


# --- The release button --------------------------------------------------------------------


def test_the_release_page_has_a_button_with_the_three_bumps():
    dispatch = workflow("release.yaml")["on"]["workflow_dispatch"]

    bump = dispatch["inputs"]["bump"]
    assert bump["type"] == "choice"
    assert bump["options"] == ["patch", "minor", "major"]
    assert bump["default"] == "patch"


def test_only_the_button_makes_a_tag_and_only_that_job_can_write():
    wf = workflow("release.yaml")
    tag = wf["jobs"]["tag"]

    assert tag["if"] == "github.event_name == 'workflow_dispatch'"
    assert tag["permissions"] == {"contents": "write"}
    run = "\n".join(s.get("run", "") for s in tag["steps"])
    assert 'scripts/next-version.sh "$BUMP"' in run
    assert "main" in run and "::error::" in run
    assert 'git push origin "refs/tags/$tag"' in run
    for name, job in wf["jobs"].items():
        if name != "tag":
            assert job["permissions"].get("contents") == "read", name


def test_promote_runs_after_the_button_and_after_a_tag_push():
    wf = workflow("release.yaml")
    promote = wf["jobs"]["promote"]

    assert promote["needs"] == "tag"
    # On a tag push, the job `tag` is skipped. promote must run then too.
    assert "needs.tag.result == 'skipped'" in promote["if"]
    assert "needs.tag.result == 'success'" in promote["if"]
    assert promote["env"]["RELEASE_SHA"] == "${{ needs.tag.outputs.sha || github.sha }}"
    assert promote["env"]["RELEASE_TAG"] == "${{ needs.tag.outputs.tag || github.ref_name }}"
    assert promote["steps"][0]["with"]["ref"] == "${{ env.RELEASE_SHA }}"
    # A dispatch run has the branch, not the tag, in GITHUB_SHA and GITHUB_REF_NAME.
    text = json.dumps(promote)
    assert "GITHUB_SHA" not in text and "GITHUB_REF_NAME" not in text
    assert text.count("github.ref_name") == 1 and text.count("github.sha") == 1


def test_gitops_runs_after_promote_also_when_the_tag_job_is_skipped():
    # GitHub skips each job after a skipped job, unless the job has a condition of its own.
    gitops = workflow("release.yaml")["jobs"]["gitops"]

    assert gitops["needs"] == "promote"
    assert gitops["if"] == "${{ !cancelled() && needs.promote.result == 'success' }}"

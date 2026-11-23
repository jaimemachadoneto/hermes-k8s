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


def test_release_runs_on_a_version_tag_only():
    wf = workflow("release.yaml")

    assert wf["on"] == {"push": {"tags": ["v[0-9]+.[0-9]+.[0-9]+"]}}


def test_release_jobs_run_on_hosted_runners_with_least_privilege():
    wf = workflow("release.yaml")

    assert wf["permissions"] == {"contents": "read"}
    publish_job = wf["jobs"]["publish"]
    assert publish_job["runs-on"] == "ubuntu-latest"
    assert publish_job["permissions"] == {"contents": "read", "packages": "write"}
    assert wf["jobs"]["gitops"]["runs-on"] == "ubuntu-latest"
    assert wf["jobs"]["gitops"]["needs"] == "publish"


def test_release_scans_before_it_pushes_and_checks_first():
    text = (ROOT / ".github" / "workflows" / "release.yaml").read_text()

    assert (
        text.index("nix flake check") < text.index("trivy") < text.index("scripts/publish-image.sh")
    )
    for image in ("sandbox-image", "hermes-image"):
        assert f".#{image}" in text
    assert "ghcr.io/jaimemachadoneto/hermes-sandbox" in text
    assert re.search(r"ghcr\.io/jaimemachadoneto/hermes(?![-\w])", text)


def test_release_opens_the_pull_request_with_the_secret_of_the_other_repositories():
    text = (ROOT / ".github" / "workflows" / "release.yaml").read_text()

    assert "secrets.MY_PAT" in text
    assert "repository: jaimemachadoneto/home-ops" in text
    assert "kubernetes/apps/ai/hermes/app/helmrelease.yaml" in text
    assert "scripts/set-image-refs.py" in text


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
    for name in ("ci.yaml", "release.yaml"):
        steps = cachix_steps(workflow(name))
        assert steps, name
        for _, step in steps:
            assert step["with"]["name"] == "hermes-k8s"
            assert step["with"]["authToken"] == "${{ secrets.CACHIX_AUTH_TOKEN }}"


def test_the_cache_push_is_skipped_only_for_the_pull_request_event():
    wf = workflow("ci.yaml")
    ((_, step),) = cachix_steps(wf)
    expr = step["with"]["skipPush"]

    assert expr == "${{ github.event_name == 'pull_request' }}"
    assert "pull_request" in wf["on"]
    assert wf["on"]["push"] == {"branches": ["main"]}
    release = workflow("release.yaml")
    for _, release_step in cachix_steps(release):
        assert release_step["with"]["skipPush"] == "${{ github.event_name == 'pull_request' }}"
    assert "pull_request" not in release["on"]


def test_the_cache_filter_keeps_claude_code_and_the_images_out_of_the_cache():
    steps = cachix_steps(workflow("ci.yaml")) + cachix_steps(workflow("release.yaml"))
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


def test_the_runner_frees_disk_before_it_builds_an_image():
    for name in ("ci.yaml", "release.yaml"):
        for job_name, job in workflow(name)["jobs"].items():
            if not any(cachix_steps({"jobs": {job_name: job}})):
                continue
            runs = [s.get("run", "") for s in job["steps"]]
            first = next(i for i, s in enumerate(job["steps"]) if "rm -rf" in runs[i])
            nix = next(
                i for i, s in enumerate(job["steps"]) if "install-nix-action" in s.get("uses", "")
            )
            assert first < nix, (name, job_name)
            assert "/usr/share/dotnet" in runs[first]


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
        s
        for s in workflow("release.yaml")["jobs"]["publish"]["steps"]
        if "trivy" in s.get("run", "")
    )
    script = step["run"]

    assert "--ignorefile .trivyignore.yaml" in script
    # A failure in the first image must not hide the findings of the second image.
    assert "--exit-code 1 ||" in script
    assert script.rstrip().endswith('exit "$failed"')


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

"""Tests of scripts/promote-image.sh: it copies a tag to other tags inside one registry."""

import hashlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "manifest-of-the-image"
DIGEST = "sha256:" + hashlib.sha256(MANIFEST.encode()).hexdigest()


@pytest.fixture
def env(tmp_path):
    """A `skopeo` that records its calls. STUB_FAIL names the verb to fail."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    skopeo = bin_dir / "skopeo"
    skopeo.write_text(
        f"""#!/usr/bin/env bash
echo "skopeo $*" >> {log}
verb=$1
[ "${{STUB_FAIL:-}}" = "$verb" ] && {{ echo "boom" >&2; exit 1; }}
case "$verb" in
  login) cat > /dev/null ;;
  inspect)
    case "$*" in
      *:1.2.3*) printf '%s' "${{STUB_DEST_MANIFEST:-{MANIFEST}}}" ;;
      *) printf '%s' "{MANIFEST}" ;;
    esac ;;
esac
exit 0
"""
    )
    skopeo.chmod(skopeo.stat().st_mode | stat.S_IXUSR)
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    return (
        {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "TMPDIR": str(tmp),
            "REGISTRY_USER": "jaime",
            "REGISTRY_TOKEN": "s3cret-token",
        },
        log,
        tmp,
    )


def promote(env, *args):
    return subprocess.run(
        [str(ROOT / "scripts" / "promote-image.sh"), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_promote_copies_all_platforms_and_keeps_the_digest(env):
    e, log, _ = env

    result = promote(e, "ghcr.io/o/hermes", "sha-1", "1.2.3")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"ghcr.io/o/hermes:sha-1@{DIGEST}"
    copy = next(
        x for x in log.read_text().splitlines() if x.startswith("skopeo --insecure-policy copy")
    )
    assert "--all" in copy
    assert "--preserve-digests" in copy
    assert "docker://ghcr.io/o/hermes:sha-1 docker://ghcr.io/o/hermes:1.2.3" in copy


def test_promote_fails_when_the_new_tag_has_another_digest(env):
    e, _, _ = env

    result = promote({**e, "STUB_DEST_MANIFEST": "another"}, "ghcr.io/o/hermes", "sha-1", "1.2.3")

    assert result.returncode != 0
    assert result.stdout == ""


@pytest.mark.parametrize("failing", ["login", "inspect", "--insecure-policy"])
def test_promote_fails_when_skopeo_fails(env, failing):
    e, _, _ = env

    result = promote({**e, "STUB_FAIL": failing}, "ghcr.io/o/hermes", "sha-1", "1.2.3")

    assert result.returncode != 0
    assert result.stdout == ""


def test_promote_leaves_no_login_and_no_token_in_an_argument(env):
    e, log, tmp = env

    promote(e, "ghcr.io/o/hermes", "sha-1", "1.2.3")
    promote({**e, "STUB_FAIL": "inspect"}, "ghcr.io/o/hermes", "sha-1", "1.2.3")

    assert list(tmp.iterdir()) == []
    assert "s3cret-token" not in log.read_text()


def test_promote_needs_a_source_and_a_target_tag(env):
    e, _, _ = env

    assert promote(e, "ghcr.io/o/hermes", "sha-1").returncode != 0


def test_promote_copies_and_verifies_each_target_tag(env):
    e, log, _ = env

    result = promote(e, "ghcr.io/o/hermes", "sha-1", "1.2.3", "1.2")

    assert result.returncode == 0, result.stderr
    copies = [x for x in log.read_text().splitlines() if "copy" in x]
    assert len(copies) == 2
    assert copies[1].endswith("docker://ghcr.io/o/hermes:1.2")

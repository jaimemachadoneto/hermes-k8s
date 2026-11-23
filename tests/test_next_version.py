"""Tests of scripts/next-version.sh: the next release tag, with the rule of `deploy`."""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "next-version.sh"


def _repo(tmp_path: Path, *tags: str) -> Path:
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    for tag in tags:
        subprocess.run([*git, "tag", tag], check=True)
    return tmp_path


def _next(repo: Path, bump: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), bump], cwd=repo, capture_output=True, text=True, check=False
    )


@pytest.mark.parametrize(
    ("bump", "want"), [("patch", "v0.2.11"), ("minor", "v0.3.0"), ("major", "v1.0.0")]
)
def test_the_bump_starts_from_the_highest_version_tag(tmp_path, bump, want):
    # v0.2.10 is above v0.2.9 by version, not by text. Other tags do not count.
    repo = _repo(tmp_path, "v0.2.9", "v0.2.10", "v0.1.12", "v0.3.0-rc1", "latest")

    result = _next(repo, bump)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == want


def test_a_repository_with_no_tag_starts_at_v0_0_1(tmp_path):
    result = _next(_repo(tmp_path), "patch")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "v0.0.1"


def test_an_unknown_bump_fails(tmp_path):
    result = _next(_repo(tmp_path, "v0.0.7"), "huge")

    assert result.returncode == 2
    assert "patch, minor or major" in result.stderr

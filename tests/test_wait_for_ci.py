"""Tests of scripts/wait-for-ci.sh: the release waits for the CI run of its commit on main."""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40


@pytest.fixture
def run(tmp_path):
    """Run the script against a `gh` that gives one answer for each call. A "|" is a new line."""

    def run_with(*answers: str, wait: int = 5):
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        (tmp_path / "answers").write_text("".join(a + "\n" for a in answers))
        log = tmp_path / "calls.log"
        gh = bin_dir / "gh"
        gh.write_text(
            f"""#!/usr/bin/env bash
echo "gh $*" >> {log}
n=$(wc -l < {log})
total=$(wc -l < {tmp_path / "answers"})
[ "$n" -le "$total" ] || n=$total
line=$(sed -n "${{n}}p" {tmp_path / "answers"})
echo "$line" | tr "|" "\\n"
"""
        )
        gh.chmod(0o755)
        env = {
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "GITHUB_REPOSITORY": "owner/repo",
            "CI_WAIT_SECONDS": str(wait),
            "CI_POLL_SECONDS": "0",
        }
        result = subprocess.run(
            ["bash", str(ROOT / "scripts" / "wait-for-ci.sh"), SHA],
            check=False,
            env=env,
            capture_output=True,
            text=True,
        )
        calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    return run_with


def test_a_green_run_passes_at_once(run):
    result, calls = run("completed success")

    assert result.returncode == 0, result.stderr
    assert len(calls) == 1
    for part in ("--workflow ci.yaml", f"--commit {SHA}", "--branch main", "--event push"):
        assert part in calls[0], part


def test_the_script_waits_until_the_run_starts_and_ends(run):
    result, calls = run("", "queued ", "in_progress ", "completed success")

    assert result.returncode == 0, result.stderr
    assert len(calls) == 4


def test_a_failed_run_fails_the_release_with_a_reason(run):
    result, _ = run("in_progress ", "completed failure")

    assert result.returncode == 1
    assert "failure" in result.stderr


def test_a_cancelled_run_fails_the_release(run):
    result, _ = run("completed cancelled")

    assert result.returncode == 1


def test_the_script_stops_at_the_time_limit(run):
    result, _ = run("in_progress ", wait=1)

    assert result.returncode == 1
    assert "did not end" in result.stderr


def test_a_green_rerun_after_a_failure_passes(run):
    # Each line is one run of the commit, the newest first. A green run is enough.
    result, _ = run("completed success|completed failure")

    assert result.returncode == 0, result.stderr

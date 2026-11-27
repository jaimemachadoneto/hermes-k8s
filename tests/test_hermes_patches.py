"""The Hermes patches (ADR 0012) apply to the Hermes of the tests, and their own tests pass.

The image build applies each patch in nix/patches/ to the source of hermes-agent. This test
applies the same patch to a copy of the installed Hermes packages that the patch changes. It then
runs the tests that the patch adds, with the copy first on the path. Thus `check` fails when a
patch does not apply to the pinned Hermes, or when the patched behavior breaks.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PATCHES = sorted((Path(__file__).parent.parent / "nix" / "patches").glob("*.patch"))


def _changed_paths(patch: Path) -> list[str]:
    return [
        line.removeprefix("+++ b/").split("\t")[0]
        for line in patch.read_text().splitlines()
        if line.startswith("+++ b/")
    ]


def _copy_installed_package(name: str, root: Path) -> None:
    spec = importlib.util.find_spec(name)
    assert spec is not None and spec.submodule_search_locations, f"Hermes has no package {name}"
    source = Path(next(iter(spec.submodule_search_locations)))
    # Do not copy the bytecode: it can hide the patched source.
    shutil.copytree(source, root / name, ignore=shutil.ignore_patterns("__pycache__"))


def test_each_patch_has_tests():
    assert PATCHES, "nix/patches/ has no patch"
    for patch in PATCHES:
        tests = [p for p in _changed_paths(patch) if p.startswith("tests/")]
        assert tests, f"{patch.name} adds no test"


@pytest.mark.parametrize("patch", PATCHES, ids=lambda p: p.name)
def test_patch_applies_and_its_tests_pass(patch: Path, tmp_path: Path):
    root = tmp_path / "hermes"
    root.mkdir()
    changed = _changed_paths(patch)
    for package in sorted({p.split("/")[0] for p in changed if not p.startswith("tests/")}):
        _copy_installed_package(package, root)

    applied = subprocess.run(
        ["git", "apply", "-p1", "--verbose", str(patch)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert applied.returncode == 0, f"{patch.name} does not apply:\n{applied.stderr}"

    (root / "pytest.ini").write_text("[pytest]\n")
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "PYTHONPATH": str(root), "HERMES_HOME": str(home)}
    tests = [p for p in changed if p.startswith("tests/")]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            "pytest.ini",
            *tests,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, (
        f"the tests of {patch.name} fail:\n{result.stdout}{result.stderr}"
    )

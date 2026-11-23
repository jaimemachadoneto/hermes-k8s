"""The builder of the Nix base clears the setgid bit that fsGroup gives the volume."""

import os
import stat
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "nix" / "nix-base-build.sh"


def _setgid(path: Path) -> bool:
    return bool(path.stat().st_mode & stat.S_ISGID)


def _run(tmp_path: Path, step: str, **env: str) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "nix-store").write_text("#!/bin/sh\ncat > /dev/null\n")
    (bin_dir / "nix-store").chmod(0o755)
    (tmp_path / "registration").write_text("")
    source = tmp_path / "image-store"
    (source / "pkg").mkdir(parents=True, exist_ok=True)
    full_env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "NIX_BASE_SOURCE_STORE": str(source),
        "NIX_BASE_REGISTRATION": str(tmp_path / "registration"),
        "NIX_BASE_WORK": str(tmp_path / "work"),
        "NIX_BASE_REPOS": "",
        **env,
    }
    subprocess.run(["bash", str(SCRIPT), step], check=True, env=full_env)


def test_copy_clears_the_setgid_bit_of_the_store_and_the_build_directory(tmp_path):
    target = tmp_path / "vol"
    (target / "nix").mkdir(parents=True)
    (target / "nix").chmod(0o2775)

    _run(tmp_path, "copy", NIX_BASE_TARGET=str(target))

    assert not _setgid(target / "nix" / "store")
    assert not _setgid(target / "nix" / "var" / "nix" / "builds")
    assert _setgid(target / "nix")


def test_warm_clears_the_setgid_bit_before_the_shells_run(tmp_path):
    # warm mounts the volume at /nix. A Nix base volume from before this fix has the bit.
    root = tmp_path / "nix"
    (root / "store").mkdir(parents=True)
    root.chmod(0o2775)
    (root / "store").chmod(0o2775)

    _run(tmp_path, "warm", NIX_BASE_NIX_ROOT=str(root))

    assert not _setgid(root / "store")
    assert not _setgid(root / "var" / "nix" / "builds")

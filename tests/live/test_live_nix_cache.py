"""The Nix cache (ticket 15): a sandbox gets a Nix package from the cluster, not from the internet."""

import pytest

pytestmark = pytest.mark.live

NIX_CACHE = "http://nix-cache.system.svc.cluster.local:8501"


def test_the_nix_cache_answers_in_the_sandbox(make_env):
    env = make_env()

    result = env.execute(f"curl -s -m 15 {NIX_CACHE}/nix-cache-info")

    assert result["returncode"] == 0, result["output"]
    assert "StoreDir: /nix/store" in result["output"]


def test_the_nix_cache_is_the_first_substituter(make_env):
    env = make_env()

    result = env.execute("nix config show substituters | awk '{print $1}'")

    assert result["returncode"] == 0, result["output"]
    assert result["output"].strip() == NIX_CACHE, result["output"]


def test_a_package_that_is_not_in_the_image_comes_from_the_nix_cache(make_env):
    env = make_env(timeout=600)

    # The flake lookup of nixpkgs needs GitHub. The build of the package must
    # use the Nix cache only: `--max-jobs 0` makes a cache miss fail the build,
    # and `--option substituters` names no other cache.
    package = "nixpkgs#figlet"
    out = env.execute(f"nix eval --raw {package}.outPath")
    assert out["returncode"] == 0, out["output"]
    path = out["output"].strip().splitlines()[-1]
    assert env.execute(f"test -e {path}")["returncode"] != 0, "figlet is already in the store"

    result = env.execute(
        f"nix build --no-link --max-jobs 0 --option substituters {NIX_CACHE} {package}"
    )

    assert result["returncode"] == 0, result["output"]
    assert env.execute(f"test -e {path}")["returncode"] == 0

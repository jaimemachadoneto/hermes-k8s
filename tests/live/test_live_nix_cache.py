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

    result = env.execute("nix show-config | grep '^substituters'")

    assert f"substituters = {NIX_CACHE} " in result["output"]


def test_a_package_that_is_not_in_the_image_comes_from_the_nix_cache(make_env):
    env = make_env(timeout=600)

    # `--option substituters` names only the Nix cache, so the build fails
    # when the cache does not serve the path. figlet is not in the sandbox image.
    result = env.execute(
        f"nix build --no-link --print-out-paths --option substituters {NIX_CACHE} nixpkgs#figlet"
    )

    assert result["returncode"] == 0, result["output"]
    assert "/nix/store/" in result["output"]

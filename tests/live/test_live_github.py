"""The GitHub tokens in a real sandbox (ADR 0008): git and gh use the Secret of the profile.

It needs the ExternalSecret `hermes-sandbox-default` in the namespace and a sandbox image
that has the token script. Set HERMES_K8S_IMAGE to the new image until home-ops uses it.
Each repository is private, so the command works only with the token of its owner.
"""

import os

import pytest

pytestmark = pytest.mark.live

# The owner jaimemachadoneto uses GITHUB_TOKEN_PERSONAL. Each other owner uses GITHUB_TOKEN.
PERSONAL_REPO = os.environ.get("HERMES_K8S_PRIVATE_REPO_PERSONAL", "jaimemachadoneto/hermes-k8s")
OTHER_REPO = os.environ.get("HERMES_K8S_PRIVATE_REPO_OTHER", "jaimemachado/nix-secrets")


@pytest.mark.parametrize("repo", [PERSONAL_REPO, OTHER_REPO])
def test_git_reads_a_private_repository_of_each_owner(make_env, repo):
    env = make_env()

    result = env.execute(f"git ls-remote https://github.com/{repo}.git HEAD")

    assert result["returncode"] == 0, result["output"]
    assert "HEAD" in result["output"]


def test_gh_is_logged_in_with_the_token_of_the_secret(make_env):
    env = make_env()

    result = env.execute("gh auth status")

    assert result["returncode"] == 0, result["output"]


def test_the_secret_is_mounted_read_only(make_env):
    env = make_env()

    result = env.execute(
        "ls /run/secrets/github && ! touch /run/secrets/github/x 2>/dev/null && echo ro"
    )

    assert result["output"].split()[-1] == "ro", result["output"]

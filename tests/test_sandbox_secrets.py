"""Seam 1 through the fake cluster: the sandbox gets the GitHub tokens from the Secret of its
profile (ADR 0008), and no token goes into an exec request."""

from pathlib import Path

import pytest
from images import IMAGE
from k8s_sandbox.sandbox_spec import SECRET_PATH, build_sandbox, secret_name

ENV_SCRIPT = Path(__file__).parent.parent / "nix" / "sandbox-github-env.sh"
TOKENS = {
    "GH_TOKEN": "tok-agents",
    "GITHUB_TOKEN": "tok-agents",
    "GITHUB_TOKEN_PERSONAL": "tok-personal",
}


@pytest.fixture
def with_secret(cluster):
    """Give the fake cluster the Secret of the default profile, and the login script of the image."""
    cluster.secrets["hermes-sandbox-default"] = dict(TOKENS)
    cluster.bash_env = str(ENV_SCRIPT)
    return cluster


def test_the_sandbox_mounts_the_secret_of_its_profile_read_only(make_env, cluster):
    make_env()

    (sandbox,) = cluster.sandboxes.values()
    assert sandbox.secret_name == "hermes-sandbox-default"
    assert sandbox.secret_mount_path == SECRET_PATH == "/run/secrets/github"


def test_the_secret_name_follows_the_profile_of_hermes(make_env, cluster, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE_NAME", "work")

    make_env()

    (sandbox,) = cluster.sandboxes.values()
    assert sandbox.secret_name == "hermes-sandbox-work"


def test_a_profile_that_a_secret_name_cannot_hold_is_refused(make_env, cluster, monkeypatch):
    monkeypatch.setenv("HERMES_PROFILE_NAME", "my_profile")

    with pytest.raises(ValueError, match="profile"):
        make_env()

    assert cluster.sandboxes == {}


@pytest.mark.parametrize("profile", ["", "A", "a b", "a/b", "-a", "a-", "a" * 60])
def test_secret_name_refuses_a_bad_profile(profile):
    with pytest.raises(ValueError):
        secret_name(profile)


def test_the_sandbox_object_holds_no_token_value():
    sandbox = build_sandbox("hermes-x", image=IMAGE, labels={}, profile="work")

    assert "tok-" not in str(sandbox)
    volumes = sandbox["spec"]["podTemplate"]["spec"]["volumes"]
    assert {"secretName": "hermes-sandbox-work", "optional": True} in [
        v.get("secret") for v in volumes
    ]


def test_git_and_gh_variables_come_from_the_files_in_each_command(make_env, with_secret):
    env = make_env()

    result = env.execute('echo "$GH_TOKEN|$GITHUB_TOKEN|$GITHUB_TOKEN_PERSONAL"')

    assert result["output"].strip() == "tok-agents|tok-agents|tok-personal"


def test_a_token_is_not_in_the_exec_request(make_env, with_secret):
    env = make_env()

    env.execute('echo "$GH_TOKEN"')
    env.execute("cat", stdin_data="input")

    assert with_secret.exec_log, "the fake cluster saw no exec"
    for argv, stdin in with_secret.exec_log:
        assert not any(t in " ".join(argv) for t in TOKENS.values())
        assert stdin is None or not any(t.encode() in stdin for t in TOKENS.values())


def test_a_sandbox_without_the_secret_runs_commands_without_tokens(make_env, cluster):
    cluster.bash_env = str(ENV_SCRIPT)
    env = make_env()

    result = env.execute('echo "[${GH_TOKEN-unset}]"; echo ok')

    assert result["returncode"] == 0
    assert "[unset]" in result["output"]


def test_the_backend_hides_the_github_keys_from_commands(registered):
    assert {"GH_TOKEN", "GITHUB_TOKEN", "GITHUB_TOKEN_PERSONAL", "GIT_CONFIG_SYSTEM"} <= set(
        registered.strip_env_keys
    )

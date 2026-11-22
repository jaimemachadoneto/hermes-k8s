"""What only a real sandbox can show: Nix, no token, no route to the API server."""

import pytest

pytestmark = pytest.mark.live


def test_nix_runs_from_the_closure_that_the_init_step_copied(make_env):
    env = make_env()

    version = env.execute("nix --version")
    store = env.execute("ls /nix/store | wc -l")

    assert version["returncode"] == 0, version["output"]
    assert version["output"].startswith("nix (Nix)")
    assert int(store["output"].strip()) > 100


def test_the_pod_has_no_service_account_token(make_env):
    env = make_env()

    # The kubelet still sets KUBERNETES_SERVICE_HOST, so the test checks the token files only.
    result = env.execute(
        "test ! -e /var/run/secrets/kubernetes.io/serviceaccount "
        "&& test ! -e /run/secrets/kubernetes.io/serviceaccount && echo clean"
    )

    assert result["output"].strip() == "clean"


def test_the_pod_cannot_reach_the_api_server(make_env):
    env = make_env()

    assert env.execute("command -v curl")["returncode"] == 0
    # 10.96.0.1 is the address of the service `kubernetes`. The connection must time out or
    # be refused (curl exit 28 or 7). A reply of any kind, even 401, gives exit code 0.
    by_address = env.execute("curl -sk -m 8 -o /dev/null https://10.96.0.1:443/version")
    by_name = env.execute("curl -sk -m 8 -o /dev/null https://kubernetes.default.svc/version")

    assert by_address["returncode"] in (7, 28), by_address
    assert by_name["returncode"] in (6, 7, 28), by_name


def test_the_workspace_and_the_home_are_writable_and_the_root_is_not(make_env):
    env = make_env()

    result = env.execute("touch /workspace/a ~/b /tmp/c && ! touch /usr/x 2>/dev/null && echo ok")

    assert result["output"].strip() == "ok"


def test_the_workspace_stays_after_a_suspend(make_env, cluster):
    first = make_env("persist")
    first.execute("echo kept > /workspace/note")
    first.cleanup()

    assert cluster.get_sandbox(first.sandbox_name).suspended
    second = make_env("persist")

    assert second.execute("cat /workspace/note")["output"].strip() == "kept"

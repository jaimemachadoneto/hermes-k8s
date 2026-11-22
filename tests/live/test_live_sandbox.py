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

    result = env.execute(
        "test ! -e /var/run/secrets/kubernetes.io/serviceaccount "
        '&& test -z "$KUBERNETES_SERVICE_HOST" && echo clean'
    )

    assert result["output"].strip() == "clean"


def test_the_pod_cannot_reach_the_api_server(make_env):
    env = make_env()

    # 10.96.0.1 is the address of the service `kubernetes`. The pod must fail to connect.
    by_address = env.execute("curl -sk -m 8 -o /dev/null https://10.96.0.1:443/version")
    by_name = env.execute("curl -sk -m 8 -o /dev/null https://kubernetes.default.svc/version")

    assert by_address["returncode"] != 0
    assert by_name["returncode"] != 0


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

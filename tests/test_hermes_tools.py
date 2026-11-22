"""The factory of Hermes, seen through the Hermes tools: `terminal` and `execute_code` choose the backend."""

import json
import os

import pytest
from tools import terminal_tool as terminal_module
from tools.code_execution_tool import execute_code
from tools.code_kernel_remote import shutdown_all_remote_kernels
from tools.terminal_tool import terminal_tool
from tools.terminal_tool_lifecycle import cleanup_all_environments


@pytest.fixture
def hermes(registered, monkeypatch, tmp_path):
    """Hermes with `terminal.backend: k8s_sandbox` and an empty home."""
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("TERMINAL_ENV", registered.name)
    monkeypatch.setenv("TERMINAL_CWD", "~")
    monkeypatch.setenv("TERMINAL_TIMEOUT", "60")
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    terminal_module._active_environments.clear()
    yield
    # The code kernel of execute_code lives in the temp directory of the sandbox. The fake
    # shares that directory with the host, so stop the kernel before the test ends.
    shutdown_all_remote_kernels()
    cleanup_all_environments()
    terminal_module._active_environments.clear()


def run(command, *, session, task_id="main"):
    previous = os.environ.get("HERMES_SESSION_KEY")
    os.environ["HERMES_SESSION_KEY"] = session
    try:
        return json.loads(terminal_tool(command, task_id=task_id))
    finally:
        if previous is None:
            os.environ.pop("HERMES_SESSION_KEY", None)
        else:
            os.environ["HERMES_SESSION_KEY"] = previous


def test_each_session_gets_its_own_sandbox_and_a_subagent_shares_the_one_of_its_parent(
    hermes, cluster
):
    run("echo one > who", session="session-1")
    run("echo two > who", session="session-2")
    # delegate_task runs a subagent with its own task id inside the session of the parent.
    child = run("cat who", session="session-1", task_id="subagent-7")

    assert len(cluster.sandboxes) == 2
    assert child["output"].strip() == "one"
    assert run("cat who", session="session-2")["output"].strip() == "two"


def test_execute_code_runs_through_the_sandbox(hermes, cluster, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_KEY", "session-code")

    result = json.loads(
        execute_code(
            "import os\nprint('home', os.path.expanduser('~'))\nprint('sum', 2 + 3)", task_id="main"
        )
    )

    assert result["status"] == "success", result
    assert "sum 5" in result["output"].splitlines()
    (sandbox,) = cluster.sandboxes.values()
    # The home of the code is the home of the sandbox, not the home of the test process.
    assert f"home {sandbox.directory}" in result["output"]

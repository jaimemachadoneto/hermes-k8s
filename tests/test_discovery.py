# pyright: reportAttributeAccessIssue=false
# The registry of Hermes exports its functions with a call at import time.
"""Hermes finds the plugin in a bundled plugins directory and reads what the provider declares."""

from pathlib import Path

import pytest
from agent import terminal_env_registry as registry
from hermes_cli.plugins import PluginManager
from images import IMAGE

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugin"


@pytest.fixture
def discovered(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_K8S_SANDBOX_IMAGE", raising=False)
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(PLUGINS_DIR))
    registry._reset_for_tests()
    manager = PluginManager()
    manager.discover_and_load()
    yield manager
    registry._reset_for_tests()


def test_hermes_loads_the_backend_plugin_from_the_bundled_directory(discovered):
    loaded = discovered._plugins["k8s_sandbox"]

    assert loaded.manifest.kind == "backend"
    assert loaded.enabled, loaded.error
    assert registry.plugin_backend_names() == ["k8s_sandbox"]


def test_the_provider_declares_how_hermes_treats_the_backend(discovered):
    flag = registry.provider_flag
    name = "k8s_sandbox"

    assert flag(name, "is_remote") is True
    assert flag(name, "is_container") is True
    assert flag(name, "session_isolated_when_nonpersistent") is True
    # Hermes strips these keys from each command that it starts (ADR 0008).
    assert registry.plugin_strip_env_keys() >= {"GH_TOKEN", "GITHUB_TOKEN", "GITHUB_TOKEN_PERSONAL"}


def test_without_a_sandbox_image_the_backend_is_not_available(discovered):
    assert registry.get_provider("k8s_sandbox").is_available() is False


def test_with_a_sandbox_image_the_backend_is_available_and_makes_no_cluster_call(
    tmp_path, monkeypatch
):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(PLUGINS_DIR))
    monkeypatch.setenv("HERMES_K8S_SANDBOX_IMAGE", IMAGE)
    registry._reset_for_tests()
    PluginManager().discover_and_load()

    assert registry.get_provider("k8s_sandbox").is_available() is True
    registry._reset_for_tests()

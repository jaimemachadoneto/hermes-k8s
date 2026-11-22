# pyright: reportAttributeAccessIssue=false
# The registry of Hermes exports its functions with a call at import time.
"""Service allowances (ADR 0007): the setting of a profile becomes labels on the sandbox pod."""

from pathlib import Path

import pytest
import yaml
from agent import terminal_env_registry as registry
from hermes_cli.plugins import PluginManager
from images import IMAGE
from k8s_sandbox.allowances import allowance_labels, services_for_profile
from k8s_sandbox.fake_cluster import FakeCluster
from k8s_sandbox.provider import K8sSandboxProvider
from k8s_sandbox.sandbox_spec import build_sandbox
from vap_rules import violations

PLUGINS_DIR = Path(__file__).resolve().parents[1] / "plugin"
LABEL = "hermes-k8s/allow-litellm"


def _sandbox_of(provider, cluster):
    provider.create_environment(cwd="/tmp", timeout=5, task_id="t")
    (sandbox,) = cluster.sandboxes.values()
    return sandbox


def test_a_profile_with_an_allowance_gets_its_label_on_the_sandbox(tmp_path):
    cluster = FakeCluster(root=tmp_path)
    provider = K8sSandboxProvider(
        cluster_factory=lambda: cluster, image=IMAGE, allowances=lambda: ["litellm"]
    )

    sandbox = _sandbox_of(provider, cluster)

    assert sandbox.labels[LABEL] == "true"
    assert sandbox.labels["hermes-task"] == sandbox.name


def test_no_setting_gives_no_allowance_label(tmp_path):
    cluster = FakeCluster(root=tmp_path)
    provider = K8sSandboxProvider(cluster_factory=lambda: cluster, image=IMAGE)

    sandbox = _sandbox_of(provider, cluster)

    assert [key for key in sandbox.labels if key.startswith("hermes-k8s/")] == []


def test_the_pod_template_of_the_sandbox_carries_each_label():
    labels = {"hermes-task": "hermes-abc", **allowance_labels(["litellm"])}

    sandbox = build_sandbox("hermes-abc", image=IMAGE, labels=labels)

    assert sandbox["spec"]["podTemplate"]["metadata"]["labels"][LABEL] == "true"
    assert violations(sandbox) == []


def test_the_admission_policy_refuses_a_label_that_it_does_not_know():
    labels = {"hermes-task": "hermes-abc", **allowance_labels(["unknown-service"])}
    other = {"hermes-task": "hermes-abc", "app.kubernetes.io/name": "litellm"}

    for bad in (labels, other):
        sandbox = build_sandbox("hermes-abc", image=IMAGE, labels=bad)
        assert violations(sandbox) == ["labels"]


def test_the_services_of_a_profile_come_from_the_map_of_the_setting():
    settings = {"work": ["litellm", "grafana"], "home": ["litellm"]}

    assert services_for_profile(settings, "work") == ["litellm", "grafana"]
    assert services_for_profile(settings, "default") == []


@pytest.mark.parametrize("settings", [None, {}, "litellm", ["litellm"], {"work": None}])
def test_a_missing_or_malformed_setting_gives_no_service(settings):
    assert services_for_profile(settings, "work") == []


def test_a_service_list_that_is_not_a_list_of_names_is_refused():
    with pytest.raises(ValueError, match="service_allowances"):
        services_for_profile({"work": "litellm"}, "work")
    with pytest.raises(ValueError, match="service_allowances"):
        services_for_profile({"work": [1]}, "work")


@pytest.mark.parametrize("name", ["", "Lite LLM", "a/b", "-x", "x-", "a" * 60, "x=true"])
def test_a_service_name_that_is_not_a_label_value_is_refused(name):
    with pytest.raises(ValueError, match="service"):
        allowance_labels([name])


def test_the_labels_have_one_entry_for_each_service():
    assert allowance_labels(["litellm", "litellm", "searx"]) == {
        "hermes-k8s/allow-litellm": "true",
        "hermes-k8s/allow-searx": "true",
    }
    assert allowance_labels([]) == {}


@pytest.fixture
def discovered(tmp_path, monkeypatch):
    """Hermes loads the plugin with a config file in its home."""

    def load(settings, profile):
        home = tmp_path / "hermes-home"
        home.mkdir(exist_ok=True)
        config = {
            "plugins": {
                "entries": {"k8s_sandbox": {"settings": settings}},
                "enabled": ["k8s_sandbox"],
            }
        }
        (home / "config.yaml").write_text(yaml.safe_dump(config))
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(PLUGINS_DIR))
        monkeypatch.setenv("HERMES_PROFILE_NAME", profile)
        monkeypatch.setenv("HERMES_K8S_SANDBOX_IMAGE", IMAGE)
        registry._reset_for_tests()
        PluginManager().discover_and_load()
        return registry.get_provider("k8s_sandbox")

    yield load
    registry._reset_for_tests()


def test_the_plugin_reads_the_setting_of_the_current_profile_from_the_config_of_hermes(discovered):
    provider = discovered({"service_allowances": {"work": ["litellm"], "home": ["x"]}}, "work")

    assert provider.service_allowances() == ["litellm"]


def test_the_plugin_gives_no_allowance_to_a_profile_that_the_setting_does_not_name(discovered):
    provider = discovered({"service_allowances": {"work": ["litellm"]}}, "other")

    assert provider.service_allowances() == []


def test_the_plugin_gives_no_allowance_when_the_config_has_no_setting(discovered):
    assert discovered({}, "work").service_allowances() == []

"""Tests of scripts/prune-versions.py and its workflow: the rule that picks the versions to delete."""

import datetime as dt
import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NOW = dt.datetime(2026, 12, 1, 12, 0, tzinfo=dt.UTC)
SHA = "sha-" + "1" * 40


def load():
    path = ROOT / "scripts" / "prune-versions.py"
    spec = importlib.util.spec_from_file_location("prune_versions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prune = load()


def version(id_, tags, days_old):
    created = NOW - dt.timedelta(days=days_old)
    return {
        "id": id_,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "metadata": {"package_type": "container", "container": {"tags": tags}},
    }


def picked(versions, **kwargs):
    return [v["id"] for v in prune.select_deletable(versions, NOW, **kwargs)]


def test_an_old_version_with_only_a_commit_tag_is_deleted():
    assert picked([version(1, [SHA], 31)]) == [1]


def test_a_version_with_a_release_tag_is_never_deleted():
    versions = [
        version(1, [SHA, "1.2.3"], 400),
        version(2, ["1.2.3"], 400),
        version(3, ["0.0.1", SHA], 400),
    ]

    assert picked(versions) == []


@pytest.mark.parametrize("days_old", [0, 1, 29, 29.99])
def test_a_version_younger_than_30_days_is_never_deleted(days_old):
    assert picked([version(1, [SHA], days_old)]) == []


def test_a_version_of_exactly_30_days_is_kept():
    assert picked([version(1, [SHA], 30)]) == []


@pytest.mark.parametrize(
    "tags",
    [
        [],  # an untagged version can be a platform image or an attestation of another version
        ["latest"],
        ["sha256-" + "a" * 64],  # the tag of an attestation
        [SHA, "latest"],
        ["sha-abc"],
        ["sha-" + "1" * 40 + "x"],
        ["xsha-" + "1" * 40],
        ["sha-" + "G" * 40],
        ["v1.2.3"],
    ],
)
def test_a_version_with_any_other_tag_or_no_tag_is_kept(tags):
    assert picked([version(1, tags, 400)]) == []


def test_two_commit_tags_on_one_old_version_are_deleted():
    assert picked([version(1, [SHA, "sha-" + "2" * 40], 90)]) == [1]


def test_a_version_with_no_metadata_or_a_bad_date_is_kept():
    broken = [
        {"id": 1, "created_at": "2020-01-01T00:00:00Z"},
        {"id": 2, "created_at": "not a date", "metadata": {"container": {"tags": [SHA]}}},
        {"id": 3, "metadata": {"container": {"tags": [SHA]}}},
        {"id": 4, "created_at": "2020-01-01T00:00:00Z", "metadata": {"container": {"tags": None}}},
    ]

    assert picked(broken) == []


def test_the_age_limit_cannot_go_below_one_day():
    with pytest.raises(ValueError):
        prune.select_deletable([], NOW, days=0)


class FakeApi:
    def __init__(self, versions):
        self.versions = versions
        self.deleted = []

    def list_versions(self):
        return self.versions

    def delete_version(self, id_):
        self.deleted.append(id_)


def test_run_deletes_only_the_selected_versions():
    api = FakeApi([version(1, [SHA], 40), version(2, [SHA, "1.0.0"], 40), version(3, [SHA], 2)])

    count = prune.run(api, NOW, dry_run=False)

    assert api.deleted == [1]
    assert count == 1


def test_a_dry_run_deletes_nothing():
    api = FakeApi([version(1, [SHA], 40)])

    count = prune.run(api, NOW, dry_run=True)

    assert api.deleted == []
    assert count == 1


def test_the_api_lists_with_the_token_and_deletes_by_id(monkeypatch):
    calls = []

    class Response:
        def __init__(self, body, link=""):
            self.body, self.headers = body, {"Link": link} if link else {}

        def read(self):
            return json.dumps(self.body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    pages = {
        "https://api.github.com/orgs/o/packages/container/hermes/versions?per_page=100": Response(
            [{"id": 1}],
            '<https://api.github.com/orgs/o/packages/container/hermes/versions?per_page=100&page=2>; rel="next"',
        ),
        "https://api.github.com/orgs/o/packages/container/hermes/versions?per_page=100&page=2": Response(
            [{"id": 2}]
        ),
        "https://api.github.com/orgs/o/packages/container/hermes/versions/7": Response(None),
    }

    def fake_open(request, timeout):
        calls.append((request.get_method(), request.full_url, request.get_header("Authorization")))
        return pages[request.full_url]

    monkeypatch.setattr(prune.urllib.request, "urlopen", fake_open)
    api = prune.GithubApi("o", "hermes", "tok")

    assert [v["id"] for v in api.list_versions()] == [1, 2]
    api.delete_version(7)

    assert calls[-1][0] == "DELETE"
    assert all(c[2] == "Bearer tok" for c in calls)


def test_the_api_url_of_a_user_owner():
    assert prune.GithubApi("o", "p", "t", owner_type="users").base.startswith(
        "https://api.github.com/users/o/packages/container/p/versions"
    )


# --- the workflow ---------------------------------------------------------------------------


def workflow():
    data = yaml.safe_load((ROOT / ".github" / "workflows" / "retention.yaml").read_text())
    data["on"] = data.pop(True, data.get("on"))
    return data


def test_the_retention_runs_every_week_and_on_request():
    wf = workflow()

    assert re.fullmatch(r"\d+ \d+ \* \* \d", wf["on"]["schedule"][0]["cron"])
    assert "workflow_dispatch" in wf["on"]
    assert "push" not in wf["on"]
    assert "pull_request" not in wf["on"]


def test_the_retention_has_the_least_token_that_works():
    wf = workflow()

    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["prune"]
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    text = (ROOT / ".github" / "workflows" / "retention.yaml").read_text()
    assert "secrets.GITHUB_TOKEN" in text
    assert "secrets.MY_PAT" not in text


def test_the_retention_covers_both_packages_and_a_manual_run_is_a_dry_run_by_default():
    wf = workflow()
    text = (ROOT / ".github" / "workflows" / "retention.yaml").read_text()

    assert "for package in hermes hermes-sandbox" in text
    assert wf["on"]["workflow_dispatch"]["inputs"]["dry-run"]["default"] is True

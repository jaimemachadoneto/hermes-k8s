"""The TTL of a suspended sandbox (ticket 14): a sandbox archive, then the delete.

The fake cluster and the test clock replace the cluster and time. A sandbox expires when it is
suspended and its last use is more than `EXPIRE_SECONDS` old. The reaper takes a sandbox archive,
waits until the archive is ready, and only then deletes the sandbox. It deletes each sandbox
archive after `ARCHIVE_KEEP_SECONDS`.
"""

import logging

import pytest
from k8s_sandbox.archive import ARCHIVE_KEEP_SECONDS, EXPIRE_SECONDS, reap_expired
from k8s_sandbox.cluster import RUNNING, SUSPENDED, ClusterError, QuotaExceeded

DAY = 24 * 3600.0


def suspended_sandbox(make_env, cluster, task_id="a"):
    env = make_env(task_id)
    env.execute("echo hi")
    env.cleanup()
    assert cluster.get_sandbox(env.sandbox_name).operating_mode == SUSPENDED
    return env.sandbox_name


def test_the_ttl_is_seven_days_and_an_archive_stays_thirty_days():
    assert EXPIRE_SECONDS == 7 * DAY
    assert ARCHIVE_KEEP_SECONDS == 30 * DAY


def test_an_expired_sandbox_gets_an_archive_and_then_the_delete(make_env, cluster, clock):
    name = suspended_sandbox(make_env, cluster)
    clock.advance(EXPIRE_SECONDS + 1)

    result = reap_expired(cluster, now=clock())

    assert result.deleted == [name]
    assert cluster.get_sandbox(name) is None
    [archive] = cluster.list_archives()
    assert archive.sandbox == name
    assert archive.ready
    assert archive.expires_at == pytest.approx(clock() + ARCHIVE_KEEP_SECONDS)
    # The archive came first: the delete of a Sandbox deletes its volume.
    assert cluster.events == [("archive", name), ("delete", name)]


def test_a_sandbox_that_is_not_old_enough_stays(make_env, cluster, clock):
    name = suspended_sandbox(make_env, cluster)
    clock.advance(EXPIRE_SECONDS - 1)

    assert reap_expired(cluster, now=clock()).deleted == []
    assert cluster.get_sandbox(name) is not None
    assert cluster.list_archives() == []


def test_a_running_sandbox_is_not_touched_however_old_its_last_use(make_env, cluster, clock):
    env = make_env("a")
    env.execute("echo hi")
    clock.advance(EXPIRE_SECONDS * 2)

    assert reap_expired(cluster, now=clock()).deleted == []
    assert cluster.get_sandbox(env.sandbox_name).operating_mode == RUNNING
    assert cluster.list_archives() == []


def test_a_sandbox_with_no_last_use_is_not_touched(cluster, clock):
    cluster.add_sandbox("hermes-x", mode=SUSPENDED, last_used=None)
    clock.advance(EXPIRE_SECONDS * 2)

    assert reap_expired(cluster, now=clock()).deleted == []
    assert cluster.get_sandbox("hermes-x") is not None


def test_a_sandbox_that_a_command_resumed_after_the_list_is_not_touched(cluster, clock):
    cluster.add_sandbox("hermes-x", mode=SUSPENDED, last_used=clock())
    clock.advance(EXPIRE_SECONDS + 1)
    # The list is old: a command resumed the sandbox before the reaper reads it again.
    cluster.before_get = lambda name: cluster.set_operating_mode(name, RUNNING)

    assert reap_expired(cluster, now=clock()).deleted == []
    assert cluster.get_sandbox("hermes-x") is not None
    assert cluster.list_archives() == []


def test_an_archive_that_is_not_ready_keeps_the_sandbox(make_env, cluster, clock, caplog):
    name = suspended_sandbox(make_env, cluster)
    cluster.archives_ready = False
    clock.advance(EXPIRE_SECONDS + 1)

    with caplog.at_level(logging.WARNING):
        assert reap_expired(cluster, now=clock(), archive_timeout=0.01).deleted == []
    assert cluster.get_sandbox(name) is not None
    assert name in caplog.text


def test_a_ready_archive_of_an_earlier_run_is_used_again(make_env, cluster, clock):
    name = suspended_sandbox(make_env, cluster)
    clock.advance(EXPIRE_SECONDS + 1)
    cluster.archive_sandbox(name, expires_at=clock() + ARCHIVE_KEEP_SECONDS, timeout=1)
    cluster.events.clear()

    assert reap_expired(cluster, now=clock()).deleted == [name]
    assert len(cluster.list_archives()) == 1
    assert cluster.events == [("delete", name)]


def test_a_full_snapshot_quota_keeps_the_sandbox_and_the_sweep_continues(
    make_env, cluster, clock, caplog
):
    first = suspended_sandbox(make_env, cluster, "a")
    second = suspended_sandbox(make_env, cluster, "b")
    clock.advance(EXPIRE_SECONDS + 1)
    cluster.archive_error = {first: QuotaExceeded("the quota of volumesnapshots is full")}

    with caplog.at_level(logging.WARNING):
        result = reap_expired(cluster, now=clock())

    assert result.deleted == [second]
    assert cluster.get_sandbox(first) is not None
    assert "quota" in caplog.text


def test_an_old_archive_is_deleted(cluster, clock):
    cluster.add_sandbox("hermes-x", mode=SUSPENDED, last_used=clock())
    cluster.archive_sandbox("hermes-x", expires_at=clock() + ARCHIVE_KEEP_SECONDS, timeout=1)
    cluster.delete_sandbox("hermes-x")
    [archive] = cluster.list_archives()

    clock.advance(ARCHIVE_KEEP_SECONDS - 1)
    assert reap_expired(cluster, now=clock()).deleted_archives == []

    clock.advance(2)
    assert reap_expired(cluster, now=clock()).deleted_archives == [archive.name]
    assert cluster.list_archives() == []


def test_a_failed_list_of_the_archives_still_reaps_nothing_wrong(cluster, clock, caplog):
    cluster.add_sandbox("hermes-x", mode=SUSPENDED, last_used=clock())
    clock.advance(EXPIRE_SECONDS + 1)
    cluster.list_archives_error = ClusterError("the API server is down")

    with caplog.at_level(logging.WARNING):
        result = reap_expired(cluster, now=clock())

    # With no list, the reaper cannot know which archive exists. It keeps each sandbox.
    assert result.deleted == []
    assert cluster.get_sandbox("hermes-x") is not None
    assert "archives" in caplog.text


def test_the_plugin_runs_the_reaper_at_start_and_then_at_each_interval(monkeypatch, cluster):
    import threading

    import k8s_sandbox

    runs = []
    stop = threading.Event()

    def reap(c, *, now):
        runs.append(c)
        if len(runs) == 3:
            stop.set()
        return k8s_sandbox.archive.ReapResult()

    monkeypatch.setattr(k8s_sandbox, "_cluster", lambda: cluster)
    monkeypatch.setattr(k8s_sandbox, "reap_expired", reap)

    k8s_sandbox._maintain(stop, interval=0.01)

    assert runs == [cluster, cluster, cluster]


def test_a_failed_run_of_the_reaper_is_logged_and_the_next_run_still_comes(
    monkeypatch, cluster, caplog
):
    import threading

    import k8s_sandbox

    runs = []
    stop = threading.Event()

    def reap(c, *, now):
        runs.append(1)
        if len(runs) == 2:
            stop.set()
        raise RuntimeError("the API server is down")

    monkeypatch.setattr(k8s_sandbox, "_cluster", lambda: cluster)
    monkeypatch.setattr(k8s_sandbox, "reap_expired", reap)

    with caplog.at_level(logging.WARNING):
        k8s_sandbox._maintain(stop, interval=0.01)

    assert len(runs) == 2
    assert "Could not reap the expired sandboxes" in caplog.text


def test_each_new_sandbox_records_its_task_and_profile(make_env, cluster):
    env = make_env("session:abc")
    annotations = cluster.sandboxes[env.sandbox_name].annotations
    assert annotations["hermes-k8s/task"] == "session:abc"
    assert annotations["hermes-k8s/profile"] == "default"

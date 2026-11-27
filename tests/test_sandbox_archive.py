"""The TTL of a suspended sandbox (ticket 14): a sandbox archive, then the delete.

The fake cluster and the test clock replace the cluster and time. A sandbox expires when it is
suspended and its last use is more than `EXPIRE_SECONDS` old. The reaper takes a sandbox archive,
waits until the archive is ready, and only then deletes the sandbox. It deletes each sandbox
archive after `ARCHIVE_KEEP_SECONDS`.
"""

import logging
import threading

import pytest
from k8s_sandbox.archive import ARCHIVE_KEEP_SECONDS, EXPIRE_SECONDS, ReapResult, reap_expired
from k8s_sandbox.cluster import RUNNING, SUSPENDED, ClusterError, QuotaExceeded

DAY = 24 * 3600.0


def suspended_sandbox(make_env, cluster, task_id="a"):
    env = make_env(task_id)
    env.execute("echo hi")
    env.cleanup()
    assert cluster.get_sandbox(env.sandbox_name).operating_mode == SUSPENDED
    return env.sandbox_name


def kinds(cluster):
    """The archives and the deletes of sandboxes, in order."""
    return [e for e in cluster.events if e[0] in ("archive", "delete")]


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
    assert kinds(cluster) == [("archive", name), ("delete", name)]


def test_a_sandbox_suspended_for_exactly_seven_days_is_not_yet_expired(make_env, cluster, clock):
    name = suspended_sandbox(make_env, cluster)
    clock.advance(EXPIRE_SECONDS)

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


def test_a_suspended_sandbox_with_no_last_use_gets_one_and_expires_later(cluster, clock):
    cluster.add_sandbox("hermes-x", mode=SUSPENDED, last_used=None)
    clock.advance(EXPIRE_SECONDS * 2)

    assert reap_expired(cluster, now=clock()).deleted == []
    assert cluster.get_sandbox("hermes-x").last_used == clock()

    clock.advance(EXPIRE_SECONDS + 1)
    assert reap_expired(cluster, now=clock()).deleted == ["hermes-x"]


def test_a_sandbox_that_the_plugin_did_not_make_is_not_touched(cluster, clock):
    cluster.add_sandbox("other", mode=SUSPENDED, last_used=clock(), managed=False)
    clock.advance(EXPIRE_SECONDS * 2)

    assert reap_expired(cluster, now=clock()).deleted == []
    assert cluster.get_sandbox("other") is not None
    assert cluster.list_archives() == []


def test_a_sandbox_that_a_command_resumed_after_the_list_is_not_touched(cluster, clock):
    cluster.add_sandbox("hermes-x", mode=SUSPENDED, last_used=clock())
    clock.advance(EXPIRE_SECONDS + 1)
    # The list is old: a command resumed the sandbox before the reaper reads it again.
    cluster.before_get = lambda name: cluster.set_operating_mode(name, RUNNING)

    assert reap_expired(cluster, now=clock()).deleted == []
    assert cluster.get_sandbox("hermes-x") is not None
    assert cluster.list_archives() == []


def test_a_sandbox_that_a_command_resumes_during_the_archive_is_kept(cluster, clock):
    cluster.add_sandbox("hermes-x", mode=SUSPENDED, last_used=clock())
    clock.advance(EXPIRE_SECONDS + 1)

    def resume(name):
        cluster.mark_used(name, clock())
        cluster.set_operating_mode(name, RUNNING)

    cluster.during_archive = resume

    assert reap_expired(cluster, now=clock()).deleted == []
    assert cluster.get_sandbox("hermes-x").operating_mode == RUNNING


def test_a_change_between_the_last_read_and_the_delete_stops_the_delete(cluster, clock, caplog):
    cluster.add_sandbox("hermes-x", mode=SUSPENDED, last_used=clock())
    clock.advance(EXPIRE_SECONDS + 1)
    # A command resumes the sandbox after the reaper read it for the last time.
    cluster.before_delete = lambda name: cluster.set_operating_mode(name, RUNNING)

    with caplog.at_level(logging.WARNING):
        assert reap_expired(cluster, now=clock()).deleted == []
    assert cluster.get_sandbox("hermes-x").operating_mode == RUNNING
    assert "hermes-x" in caplog.text


def test_a_task_that_comes_back_gets_a_new_archive_at_its_next_expiry(make_env, cluster, clock):
    name = suspended_sandbox(make_env, cluster)
    clock.advance(EXPIRE_SECONDS + 1)
    reap_expired(cluster, now=clock())
    [first] = cluster.list_archives()

    # The same task comes back. Its sandbox has the same name, and new work goes into it.
    clock.advance(2 * DAY)
    assert suspended_sandbox(make_env, cluster) == name
    clock.advance(EXPIRE_SECONDS + 1)
    cluster.events.clear()

    assert reap_expired(cluster, now=clock()).deleted == [name]
    # The old archive does not hold the new work, so the reaper made a second archive first.
    assert kinds(cluster) == [("archive", name), ("delete", name)]
    assert {a.name for a in cluster.list_archives()} - {first.name}


def test_an_expired_archive_and_a_new_expiry_of_its_sandbox_in_one_run(make_env, cluster, clock):
    name = suspended_sandbox(make_env, cluster)
    clock.advance(EXPIRE_SECONDS + 1)
    reap_expired(cluster, now=clock())
    [old] = cluster.list_archives()

    clock.advance(ARCHIVE_KEEP_SECONDS - EXPIRE_SECONDS - 2 * DAY)
    suspended_sandbox(make_env, cluster)
    clock.advance(EXPIRE_SECONDS + 3 * DAY)

    result = reap_expired(cluster, now=clock())

    assert result.deleted == [name]
    assert result.deleted_archives == [old.name]
    # The new work still has a copy.
    [new] = cluster.list_archives()
    assert new.sandbox == name and new.ready


def test_an_archive_that_is_not_ready_is_waited_for_and_never_made_twice(
    make_env, cluster, clock, caplog
):
    name = suspended_sandbox(make_env, cluster)
    cluster.archives_ready = False
    clock.advance(EXPIRE_SECONDS + 1)

    with caplog.at_level(logging.WARNING):
        assert reap_expired(cluster, now=clock()).deleted == []
        clock.advance(3600)
        assert reap_expired(cluster, now=clock()).deleted == []
    assert cluster.get_sandbox(name) is not None
    assert len(cluster.list_archives()) == 1
    assert name in caplog.text

    cluster.archives_ready = True
    clock.advance(3600)
    assert reap_expired(cluster, now=clock()).deleted == [name]
    assert len(cluster.list_archives()) == 1


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


def test_an_archive_is_deleted_when_its_expiry_comes(cluster, clock):
    cluster.add_sandbox("hermes-x", mode=SUSPENDED, last_used=clock())
    cluster.archive_sandbox(
        "hermes-x", last_used=clock(), expires_at=clock() + ARCHIVE_KEEP_SECONDS, timeout=1
    )
    cluster.delete_sandbox("hermes-x")
    [archive] = cluster.list_archives()

    clock.advance(ARCHIVE_KEEP_SECONDS - 1)
    assert reap_expired(cluster, now=clock()).deleted_archives == []

    clock.advance(1)
    assert reap_expired(cluster, now=clock()).deleted_archives == [archive.name]
    assert cluster.list_archives() == []


def test_an_archive_with_no_expiry_is_kept(cluster, clock):
    cluster.add_archive("odd", sandbox="hermes-x", expires_at=None)
    clock.advance(ARCHIVE_KEEP_SECONDS * 2)

    assert reap_expired(cluster, now=clock()).deleted_archives == []
    assert [a.name for a in cluster.list_archives()] == ["odd"]


def test_a_failed_delete_of_one_archive_does_not_stop_the_others(cluster, clock, caplog):
    cluster.add_archive("first", sandbox="hermes-x", expires_at=clock())
    cluster.add_archive("second", sandbox="hermes-y", expires_at=clock())
    cluster.delete_archive_error = {"first": ClusterError("the API server is down")}

    with caplog.at_level(logging.WARNING):
        result = reap_expired(cluster, now=clock())

    assert result.deleted_archives == ["second"]
    assert "first" in caplog.text


def test_a_failed_list_of_the_archives_still_reaps_the_sandboxes(cluster, clock, caplog):
    cluster.add_sandbox("hermes-x", mode=SUSPENDED, last_used=clock())
    clock.advance(EXPIRE_SECONDS + 1)
    cluster.list_archives_error = ClusterError("the API server is down")

    with caplog.at_level(logging.WARNING):
        result = reap_expired(cluster, now=clock())

    # The name of an archive comes from the sandbox and its last use, so no list is needed
    # to make only one archive of each sandbox.
    assert result.deleted == ["hermes-x"]
    assert "archives" in caplog.text


# --- the thread of the plugin ---------------------------------------------------------------


def test_the_plugin_runs_the_reaper_at_start_and_then_at_each_interval(monkeypatch, cluster):
    import k8s_sandbox

    calls = []
    stop = threading.Event()

    def reap(c, *, now):
        calls.append((c, now))
        if len(calls) == 3:
            stop.set()
        return ReapResult()

    monkeypatch.setattr(k8s_sandbox, "_cluster", lambda: cluster)
    monkeypatch.setattr(k8s_sandbox, "reap_expired", reap)
    monkeypatch.setattr(k8s_sandbox.time, "time", lambda: 1234.5)

    k8s_sandbox._maintain(stop, interval=0.01)

    assert calls == [(cluster, 1234.5)] * 3


def test_a_failed_run_of_the_reaper_is_logged_and_the_next_run_still_comes(
    monkeypatch, cluster, caplog
):
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


def test_the_start_suspends_the_idle_sandboxes_before_the_first_reap(monkeypatch):
    import k8s_sandbox

    order = []
    monkeypatch.setattr(k8s_sandbox, "_suspend_left_running", lambda: order.append("suspend"))
    monkeypatch.setattr(k8s_sandbox, "_maintain", lambda stop: order.append("reap"))

    k8s_sandbox._start_sweeps()

    assert order == ["suspend", "reap"]

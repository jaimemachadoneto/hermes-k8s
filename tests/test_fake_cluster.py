"""The fake cluster must behave as a real pod does for a command that starts a background job."""

import os
import signal
import time

from images import IMAGE
from k8s_sandbox.cluster import CancelToken

BACKGROUND = "exec sleep 60 & echo $!"


def test_exec_returns_when_the_command_exits_and_leaves_a_background_job_alive(cluster):
    cluster.create_sandbox("bg", image=IMAGE, labels={})
    started = time.monotonic()
    result = cluster.exec("bg", ["bash", "-c", BACKGROUND], timeout=15, cancel=CancelToken())
    elapsed = time.monotonic() - started
    pid = int(result.output.strip())
    try:
        # A real exec call ends with the command, not with the pipe of its background job.
        assert result.exit_code == 0
        assert elapsed < 10
        # The job keeps running. The timeout of the call must not kill it.
        os.kill(pid, 0)
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_exec_returns_all_output_of_a_command_that_exits_after_a_background_job_starts(cluster):
    cluster.create_sandbox("out", image=IMAGE, labels={})
    command = "sleep 60 & echo $! >&2; seq 1 20000"
    result = cluster.exec("out", ["bash", "-c", command], timeout=15, cancel=CancelToken())
    lines = result.output.split()
    try:
        assert result.exit_code == 0
        assert lines[-1] == "20000"
        assert len(lines) == 20001
    finally:
        try:
            os.kill(int(lines[0]), signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_exec_returns_the_output_of_a_command_that_writes_late_and_exits(cluster):
    cluster.create_sandbox("late", image=IMAGE, labels={})
    for _ in range(10):
        result = cluster.exec(
            "late", ["bash", "-c", "sleep 0.05; echo done"], timeout=15, cancel=CancelToken()
        )
        assert (result.output, result.exit_code) == ("done\n", 0)


def test_exec_delivers_a_large_input_without_a_deadlock(cluster):
    cluster.create_sandbox("in", image=IMAGE, labels={})
    data = b"x" * 1_000_000
    result = cluster.exec("in", ["cat"], stdin=data, timeout=15, cancel=CancelToken())
    assert result.output == data.decode()


def test_exec_obeys_the_timeout_of_a_command_that_closes_its_output(cluster):
    cluster.create_sandbox("closed", image=IMAGE, labels={})
    started = time.monotonic()
    result = cluster.exec(
        "closed", ["bash", "-c", "exec >&- 2>&-; sleep 600"], timeout=1, cancel=CancelToken()
    )
    assert result.exit_code == 124
    assert time.monotonic() - started < 10

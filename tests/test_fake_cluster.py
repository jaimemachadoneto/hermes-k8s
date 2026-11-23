"""The fake cluster must behave as a real pod does for a command that starts a background job."""

import os
import signal
import time

from images import IMAGE
from k8s_sandbox.cluster import CancelToken

BACKGROUND = "( sleep 60 ) & echo $!"


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

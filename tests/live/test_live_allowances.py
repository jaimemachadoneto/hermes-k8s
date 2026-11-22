"""Service allowances in the real cluster (ADR 0007): the label opens the policy of one service."""

import pytest

pytestmark = pytest.mark.live

# The unauthenticated health route of LiteLLM, through its Service in the namespace `ai`.
LITELLM = "http://litellm.ai.svc.cluster.local:4000/health/liveliness"
# curl exit 28 is a time out. The deny of Cilium drops the packets, so no reply comes.
CURL = "curl -s -m 8 -o /dev/null -w '%{http_code}' " + LITELLM


def test_litellm_answers_a_sandbox_that_has_the_allowance(make_env, allowed_services, cluster):
    allowed_services.append("litellm")
    env = make_env("allowed")

    result = env.execute(CURL)

    assert result["returncode"] == 0, result
    assert result["output"].strip() == "200"


def test_litellm_does_not_answer_a_sandbox_without_the_allowance(make_env):
    env = make_env("denied")

    result = env.execute(CURL)

    assert result["returncode"] == 28, result

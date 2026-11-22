"""Hermes terminal-backend plugin: one Kubernetes sandbox for each session."""

import logging

from .provider import K8sSandboxProvider

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    logger.warning("k8s_sandbox has no cluster client yet: the backend is not available")
    # TODO: pass the factory of the real cluster client when ticket 08 adds it.
    ctx.register_terminal_environment_provider(K8sSandboxProvider())

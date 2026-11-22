"""Hermes terminal-backend plugin: one Kubernetes sandbox for each session."""

from .provider import K8sSandboxProvider


def register(ctx) -> None:
    # TODO: pass the factory of the real cluster client when ticket 08 adds it.
    ctx.register_terminal_environment_provider(K8sSandboxProvider())

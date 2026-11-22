"""The provider that registers the backend `k8s_sandbox` in Hermes."""

# pyright: reportIncompatibleMethodOverride=false, reportAssignmentType=false
# Hermes types `name` as a property and `create_environment` with no return value, but
# its own documentation sets a class attribute and returns an environment.
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from agent.terminal_env_provider import TerminalEnvironmentProvider

from .cluster import Cluster
from .environment import K8sSandboxEnvironment
from .lifecycle import Clock
from .sandbox_spec import validate_image

# The GitHub tokens stay in the Secret of the sandbox namespace (ADR 0008). Hermes
# keeps its own copies for its own features, so the backend hides them from commands.
GITHUB_KEYS = frozenset({"GH_TOKEN", "GITHUB_TOKEN", "GITHUB_TOKEN_PERSONAL"})


class K8sSandboxProvider(TerminalEnvironmentProvider):
    name = "k8s_sandbox"
    display_name = "Kubernetes sandbox"
    is_remote = True
    is_container = True
    # The plugin finds a sandbox by its name, so two non-persistent sessions must not share one.
    session_isolated_when_nonpersistent = True

    def __init__(
        self,
        cluster_factory: Callable[[], Cluster] | None = None,
        image: str = "",
        clock: Clock = time.time,
    ):
        self._cluster_factory = cluster_factory
        self._image = image
        self._clock = clock

    @property
    def description(self) -> str:
        return "Run commands in a sandbox of the Kubernetes cluster, one for each session."

    @property
    def strip_env_keys(self) -> frozenset:
        return GITHUB_KEYS

    @property
    def default_cwd(self) -> str:
        # The environment asks the sandbox for its home, the start of the workspace.
        return "~"

    def is_available(self) -> bool:
        return self._cluster_factory is not None and bool(self._image)

    def create_environment(
        self, *, cwd, timeout, task_id="default", image=None, container_config=None, **kwargs: Any
    ):
        if self._cluster_factory is None:
            raise RuntimeError("k8s_sandbox has no cluster client configured")
        # The configured image wins. Hermes passes its own container image setting here, and
        # that image is not a sandbox image.
        image = self._image or image or ""
        if not image:
            raise ValueError("k8s_sandbox has no sandbox image configured")
        # The admission policy refuses an image with no digest. Refuse it here, before the first
        # cluster call.
        validate_image(image)
        return K8sSandboxEnvironment(
            cluster=self._cluster_factory(),
            task_id=task_id,
            image=image,
            cwd=cwd,
            timeout=timeout,
            clock=self._clock,
            **({"ready_timeout": kwargs["ready_timeout"]} if "ready_timeout" in kwargs else {}),
        )

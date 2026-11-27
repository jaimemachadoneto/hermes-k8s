# An agent-sandbox Sandbox for each session, with commands through pods/exec

The plugin makes one `Sandbox` (`agents.x-k8s.io/v1beta1`, the core
controller that home-ops installs) for each session. The controller owns
the life of the pod and the volume: `operatingMode: Suspended` when the
sandbox is idle, and `shutdownTime` for the end of its life (replaced by
ADR 0013: the plugin archives and deletes an expired sandbox). The plugin
runs each command with `pods/exec` on the pod of the Sandbox, as one
`bash -c` call. The Hermes base class keeps the working directory and the
environment variables between calls.

## Considered Options

- Plain Pods and PVCs: the plugin must then do the work of the controller.
- The agent-sandbox Python client: it needs an HTTP runtime server in each
  pod, the sandbox router and the warm-pool extension. home-ops installs
  none of these.
- A warm pool: a claim with its own volume templates always starts cold,
  and each session needs its own cloned volume, so a pool gives no gain.

## Consequences

- A ValidatingAdmissionPolicy in `hermes-sandbox` limits the Sandbox spec:
  the image, no ServiceAccount token, only template or `emptyDir` volumes,
  no node `home-ops-03`.
- The plugin sends no secret with a command. The sandbox reads its secrets
  from its own namespace (ADR 0008).
- A timeout or a cancel ends the exec request on the client side only. The
  environment runs each command as a process group. Each command has its own
  directory in `/tmp/hermes-k8s`. After a timeout or a cancel, the environment
  runs a second exec that stops the group.

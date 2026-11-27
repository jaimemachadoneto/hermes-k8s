---
status: accepted
---

# The Hermes image applies a patch so that the Dashboard can restart the gateway

The Hermes pod has two containers on one volume: `app` runs the gateway, and
`dashboard` runs the Dashboard (ticket 10). Each container has its own PID
namespace. Hermes `v0.23.3` finds the gateway by its PID only. Thus the
Dashboard shows the gateway as stopped, and its Restart button runs
`hermes gateway restart --interrupt`, which cannot signal the gateway. That
command then starts a second gateway in the `dashboard` container and never
exits. A save of the Telegram settings uses the same restart.

We do not share the PID namespace of the pod. With one PID namespace,
`dashboard` and `state-sync` can read `/proc/<pid>/environ` and the files of
`app`, which hold the Claude token and the ServiceAccount token.

The gateway already has a control socket on the volume
(`/opt/data/gateway.sock`). Its verb `pause-for-update` makes the gateway
drain and exit for its supervisor. We make these changes:

- The gateway runs as `hermes gateway run --external-supervisor`. The kubelet
  is its supervisor: it starts the `app` container again after the exit.
  This is a container restart, not a new pod.
- The Dashboard reads the status of the gateway from its API server, with
  `GATEWAY_HEALTH_URL=http://127.0.0.1:8642`. Hermes supports this setting.
- A patch to Hermes: when `hermes gateway restart` cannot see the PID of the
  gateway, it asks the control socket. If the gateway that answers declares an
  external supervisor, the command sends `pause-for-update`. It then waits up
  to 120 seconds, plus the drain time, until a gateway with a new PID or start
  time answers. It never starts a gateway in its own container.

The patch is `nix/patches/gateway-restart-in-another-pid-namespace.patch`.
`nix/hermes-patched.nix` applies it to the pinned source and builds the
`default` package with the package module of the Hermes flake. Thus the
package keeps the dependency groups of upstream. The patch changes only files
of upstream and adds an upstream-style test, so the same diff can be an
upstream pull request.

## Considered Options

- A shared PID namespace (`shareProcessNamespace: true`) and
  `--external-supervisor`. This needs no patch, but it exposes the tokens of
  `app` to the other containers. We reject this option.
- The Dashboard and the gateway in one container, with a supervisor such as
  s6, as the upstream Docker image does. This needs a second process
  supervisor in the image, and the Dashboard then runs next to the tokens. We
  reject this option.
- A restart with `kubectl rollout restart`. It replaces the pod, which stops
  Hermes for 1 to 2 minutes, and it needs RBAC on the Deployment. It does not
  repair the Telegram setup of the Dashboard. We reject this option.

## Consequences

- [ADR 0009](0009-node-in-the-hermes-image.md) rejected a patch to remove node,
  because we must keep a patch for each Hermes release. This patch has the same
  cost, but there is no other safe way to restart the gateway. We accept the
  cost until upstream has a fix.
- A Hermes update must apply the patch again. If the patch does not apply, the
  image build fails. `check` applies the patch to the Hermes of the tests and
  runs the tests of the patch. The image check fails when a copy of the patched
  file in the image does not have the patch.
- The evaluation of the flake imports the package module from the patched
  source, which is a derivation (import from derivation).
- Remove the patch when upstream merges a fix.

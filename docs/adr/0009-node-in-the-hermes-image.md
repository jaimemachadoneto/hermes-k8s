---
status: accepted
supersedes: ADR-0006
---

# The Hermes image has node because the Hermes package needs it

This ADR supersedes [ADR 0006](0006-nix-built-hermes-image.md). ADR 0006
said that the Hermes image has no node. This is wrong. The `default`
package of the Hermes flake at `v0.23.3` puts `node` on the `PATH` of its
wrapper. The wrapper also sets `HERMES_NODE`. Hermes needs node to run its
terminal interface (TUI). Node is in the closure of the package, so the
image contains it. We accept this.

The rest of ADR 0006 still holds:

- The Hermes image is built with `dockerTools` from the `default` package
  of the Hermes flake at the tag that the cluster runs (`v0.23.3`). That
  package includes the Dashboard and the Webapp.
- The image adds `claude-code`. It also adds one read-only
  `HERMES_BUNDLED_PLUGINS` directory. The directory has the stock plugins,
  `claude-subscription-directsdk` at a pinned commit, and the hermes-k8s
  backend.
- The command is `hermes gateway run`, with no s6. The entrypoint is tini.

## Considered Options

- Remove node from the image. The wrapper of the package puts node on the
  `PATH`, so we must patch the package. We must keep the patch for each
  Hermes release. We reject this option.

## Consequences

- Node is in the image only because the Hermes package needs it. It is not
  a browser tool.
- The image has no Playwright Chromium, no Photon sidecar and no s6, so
  Hermes has no browser tool. If we need a browser, we put it in the
  sandbox.
- The `config-migrate` init container of the upstream image is not needed.
- Do not pin a calendar tag (`v2026.x.y`). Those tags are older than the
  `v0.2x.y` tags. `v2026.9.24` is 0.21.5.

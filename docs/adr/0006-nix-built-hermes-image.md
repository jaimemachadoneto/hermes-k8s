# The Hermes image is built with Nix from the Hermes flake

The Hermes image is built with `dockerTools` from the `default` package of
the Hermes flake at the tag that the cluster runs (`v0.23.3`). That package
includes the Dashboard and the Webapp. The image adds `claude-code` and one
read-only `HERMES_BUNDLED_PLUGINS` directory: the stock plugins,
`claude-subscription-directsdk` at a pinned commit, and the hermes-k8s
backend. The command is `hermes gateway run`, with no s6.

## Consequences

- The image has no Playwright Chromium, no Photon sidecar and no node, so
  Hermes has no browser tool. A browser goes into the sandbox if we need
  one.
- The `config-migrate` init container of the upstream image is not needed.
- Do not pin a calendar tag (`v2026.x.y`): those tags are older than the
  `v0.2x.y` tags. `v2026.9.24` is 0.21.5.

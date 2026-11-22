# hermes-k8s

## Sandbox image

The flake builds the sandbox image with `dockerTools`.

- `build` builds the image.
- `flake-check` runs `nix flake check`. The check reads the image: the user,
  the entrypoint, the tools and `nix.conf`.
- `push-image` pushes the image to `ghcr.io/jaimemachadoneto/hermes-sandbox`
  and prints the digest. The token of `gh` needs the scope `write:packages`.

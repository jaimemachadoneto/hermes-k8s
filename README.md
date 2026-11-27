# hermes-k8s

## Sandbox image

The flake builds the sandbox image with `dockerTools`.

- `build` builds the image.
- `flake-check` runs `nix flake check`. The check reads the image: the user,
  the entrypoint, the tools and `nix.conf`.
- `push-image` pushes the image to `ghcr.io/jaimemachadoneto/hermes-sandbox`
  and prints the digest. The token of `gh` needs the scope `write:packages`.

## Hermes patches

The Hermes image builds Hermes from the pinned tag with the patches in
`nix/patches/` (ADR 0012). Each patch changes only files of upstream and adds
an upstream-style test. `check` applies each patch to the Hermes of the tests
and runs its tests. When you update Hermes, run `check` and `flake-check`. If
a patch does not apply, make it again against the new tag, or remove it when
upstream has the fix.

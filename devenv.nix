{ pkgs, ... }:
{
  name = "hermes-k8s";

  # Stacks come from devenv.stacks.nix (written by `base link`), pulled in
  # by devenv.yaml. Put project-specific tools here; machine-only settings go
  # in devenv.local.nix.
  packages = with pkgs; [
    skopeo # for push-image
    kubectl # for kubeconfig-live
  ];

  # `check` stays the verb of the Python stack. This verb reads the image.
  base.commands.flake-check = [ "nix flake check" ];

  # The live suite runs against the real cluster, only on request. `kubeconfig-live` writes
  # the kubeconfig that it needs. See tests/live/conftest.py.
  base.commands.smoke-live = [
    ''
      : "''${KUBECONFIG:?run kubeconfig-live first and export KUBECONFIG}"
      HERMES_K8S_LIVE=1 python -m pytest tests/live -m live -v "$@"
    ''
  ];

  base.commands.kubeconfig-live = [ "scripts/kubeconfig-live.sh" ];

  base.commands.build = [
    "nix build .#sandbox-image --no-link"
    "nix build .#hermes-image --no-link"
  ];

  # Manual push of the images. The token of `gh` needs the scope `write:packages`.
  # `scripts/publish-image.sh` keeps the login in a temporary file, stops when
  # skopeo fails, and prints `image:tag@sha256:...`. The cluster pins an image by
  # that digest. The packages on ghcr.io stay private. CI uses the same script.
  base.commands.push-image = [
    ''
      set -eo pipefail
      out=$(nix build .#sandbox-image --no-link --print-out-paths)
      REGISTRY_USER=$(gh api user --jq .login) REGISTRY_TOKEN=$(gh auth token) \
        scripts/publish-image.sh sandbox-image ghcr.io/jaimemachadoneto/hermes-sandbox "$(basename "$out" | cut -c1-12)"
    ''
  ];

  base.commands.push-hermes-image = [
    ''
      set -eo pipefail
      out=$(nix build .#hermes-image --no-link --print-out-paths)
      REGISTRY_USER=$(gh api user --jq .login) REGISTRY_TOKEN=$(gh auth token) \
        scripts/publish-image.sh hermes-image ghcr.io/jaimemachadoneto/hermes "$(basename "$out" | cut -c1-12)"
    ''
  ];

  # `deploy patch|minor|major` tags main as vX.Y.Z and pushes the tag. The
  # workflow .github/workflows/release.yaml does the rest.
  base.release.enable = true;

  # env.DATABASE_URL = "...";
  # services.postgres.enable = true;
}

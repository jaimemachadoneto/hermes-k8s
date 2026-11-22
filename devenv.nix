{ pkgs, ... }:
{
  name = "hermes-k8s";

  # Stacks come from devenv.stacks.nix (written by `base link`), pulled in
  # by devenv.yaml. Put project-specific tools here; machine-only settings go
  # in devenv.local.nix.
  packages = with pkgs; [
    skopeo # for push-image
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

  base.commands.build = [ "nix build .#sandbox-image --no-link" ];

  # Manual push of the sandbox image. The token of `gh` needs the scope
  # `write:packages`. It goes to skopeo through stdin, never to a file. The
  # cluster pins the image by the digest that this verb prints.
  base.commands.push-image = [
    ''
      out=$(nix build .#sandbox-image --no-link --print-out-paths)
      ref="ghcr.io/jaimemachadoneto/hermes-sandbox:$(basename "$out" | cut -c1-12)"
      gh auth token | skopeo login ghcr.io -u "$(gh api user --jq .login)" --password-stdin >/dev/null
      skopeo --insecure-policy copy --quiet --digestfile /dev/stdout "docker-archive:$out" "docker://$ref" | sed "s|^|$ref@|"
    ''
  ];

  # env.DATABASE_URL = "...";
  # services.postgres.enable = true;
}

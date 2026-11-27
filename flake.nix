# The images of hermes-k8s. `packages.<system>.sandbox-image` is the sandbox
# image and `packages.<system>.hermes-image` is the Hermes image. Each check
# of the same name reads the configuration of its image.
{
  description = "Images of hermes-k8s";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    # devenv moves fast, so take it from unstable.
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";
    # ADR 0006: the tag that the cluster runs. Never a calendar tag (v2026.x.y).
    hermes.url = "github:NousResearch/hermes-agent/v0.23.3";
    # ADR 0005: the subscription plugin, fetched by commit.
    hermes-official-plugins = {
      url = "github:NousResearch/hermes-official-plugins/269ddcbc8598ece2dc5bb82115ac68954bc4024f";
      flake = false;
    };
  };

  outputs =
    {
      nixpkgs,
      nixpkgs-unstable,
      hermes,
      hermes-official-plugins,
      ...
    }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      pkgs-unstable = nixpkgs-unstable.legacyPackages.${system};
      sandbox-image = import ./nix/sandbox-image.nix { inherit pkgs pkgs-unstable; };
      # Only claude-code is unfree, and only by its name.
      pkgs-unfree = import nixpkgs-unstable {
        inherit system;
        config.allowUnfreePredicate = pkg: builtins.elem (nixpkgs.lib.getName pkg) [ "claude-code" ];
      };
      # ADR 0012: the default package of Hermes with the patches of nix/patches/.
      hermes-patched = import ./nix/hermes-patched.nix { inherit hermes system; };
      hermes-image = import ./nix/hermes-image.nix {
        inherit pkgs;
        hermes = hermes-patched;
        claude-code = pkgs-unfree.claude-code;
        subscription-plugin = hermes-official-plugins;
      };
    in
    {
      packages.${system} = {
        inherit sandbox-image hermes-image;
      };

      checks.${system} = {
        sandbox-image = import ./nix/sandbox-image-check.nix {
          inherit pkgs;
          image = sandbox-image;
        };
        nix-base-build = import ./nix/nix-base-build-check.nix {
          inherit pkgs;
          image = sandbox-image;
          inherit (sandbox-image) builder registration;
        };
        hermes-image = import ./nix/hermes-image-check.nix {
          inherit pkgs;
          image = hermes-image;
          claude-code = pkgs-unfree.claude-code;
          minClaudeVersion = "2.1.321";
          plugins = [
            "disk-cleanup:disk-cleanup"
            "security-guidance:security-guidance"
            "claude-subscription-directsdk:claude-subscription-directsdk-experimental"
            "k8s_sandbox:k8s_sandbox"
          ];
          # ADR 0012: each entry is "file in site-packages:text that only the patch adds".
          patchMarkers = [
            "hermes_cli/gateway_supervised_restart.py:def restart_foreign_supervised_gateway("
          ];
        };
      };
    };
}

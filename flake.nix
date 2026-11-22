# The images of hermes-k8s. `packages.<system>.sandbox-image` is the sandbox
# image. `checks.<system>.sandbox-image` reads its configuration.
{
  description = "Images of hermes-k8s";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    # devenv moves fast, so take it from unstable.
    nixpkgs-unstable.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    { nixpkgs, nixpkgs-unstable, ... }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      pkgs-unstable = nixpkgs-unstable.legacyPackages.${system};
      sandbox-image = import ./nix/sandbox-image.nix { inherit pkgs pkgs-unstable; };
    in
    {
      packages.${system}.sandbox-image = sandbox-image;

      checks.${system}.sandbox-image = import ./nix/sandbox-image-check.nix {
        inherit pkgs;
        image = sandbox-image;
      };
    };
}

# The sandbox image: the shared tools, Nix and devenv. The pod runs it as uid
# 10000 with a read-only root file system, and it mounts a volume at /nix and
# at /workspace. The volume at /nix hides the store of the image, so nothing
# here may depend on a Nix daemon or on state in /nix/var.
{ pkgs, pkgs-unstable }:
let
  inherit (pkgs) lib;
  uid = "10000";

  # The service of the Nix cache in home-ops (namespace system).
  nixCache = "http://nix-cache.system.svc.cluster.local:8501";
  publicKeys = [
    "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
    "devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw="
    "nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs="
  ];

  tools = with pkgs; [
    git
    gh
    openssh
    bashInteractive
    coreutils
    findutils
    gnugrep
    gnused
    gawk
    ripgrep
    fd
    jq
    curl
    gnutar
    gzip
    unzip
    which
    python3
    nix
    direnv
    pkgs-unstable.devenv
  ];

  # The builder of a Nix base (ADR 0003). The Nix base Job runs it, and the init step of a
  # sandbox does the same copy with a shell script in `sandbox_spec.py`.
  builder = pkgs.writeShellApplication {
    name = "nix-base-build";
    runtimeInputs = with pkgs; [
      coreutils
      git
      nix
      pkgs-unstable.devenv
    ];
    text = builtins.readFile ./nix-base-build.sh;
  };

  contents = tools ++ [
    builder
    pkgs.dockerTools.binSh
    pkgs.dockerTools.caCertificates
    pkgs.tini
  ];

  # The Nix database of the volume lists no path at the start. This file lists each store path
  # of the image, so that Nix keeps the paths that the copy brings instead of fetching them.
  registration = "${pkgs.closureInfo { rootPaths = contents; }}/registration";
in
pkgs.dockerTools.buildLayeredImage {
  name = "ghcr.io/jaimemachadoneto/hermes-sandbox";
  inherit contents;
  extraCommands = ''
    mkdir -p usr/bin workspace tmp home/sandbox etc/nix etc/sandbox run/secrets/github

    # These are real files, not links into the store: the volume at /nix hides
    # the store, and the user lookup must work before the Nix base is ready.
    # Kubernetes resolves the user by number, but some CLIs read the name and
    # the home directory from /etc/passwd.
    printf '%s\n' 'root:x:0:0::/root:/bin/sh' \
      'sandbox:x:${uid}:${uid}::/home/sandbox:/bin/bash' > etc/passwd
    printf '%s\n' 'root:x:0:' 'sandbox:x:${uid}:' > etc/group

    # No daemon and no build users: Nix runs as one user. The Nix sandbox
    # needs user namespaces, which the pod does not allow (ADR 0003).
    #
    # The Nix cache (ncps) is the first substituter. The three public caches
    # stay as a fallback. A sandbox then works when the Nix cache is down.
    # ncps passes the upstream signatures through and has no key of its own.
    # Trust only the keys of the upstream caches.
    printf '%s\n' 'sandbox = false' 'build-users-group =' \
      'experimental-features = nix-command flakes' \
      'substituters = ${nixCache} https://cache.nixos.org https://devenv.cachix.org https://nix-community.cachix.org' \
      'trusted-public-keys = ${lib.concatStringsSep " " publicKeys}' \
      > etc/nix/nix.conf

    # The GitHub tokens (ADR 0008). The plugin mounts the Secret of the profile at
    # /run/secrets/github. A command is `bash -c`, not a login shell, so BASH_ENV
    # (see Env) makes bash read the script for each command, and /etc/profile reads
    # it for a login shell. git has no need of the script: its credential helpers read
    # the files. The system file of git is in the store by default, so Env sets it.
    cp ${./sandbox-github-env.sh} etc/sandbox/github-env.sh
    cp ${./sandbox-gitconfig} etc/gitconfig
    printf '%s\n' '. /etc/sandbox/github-env.sh' > etc/profile

    # The init step of a sandbox and the builder read this file.
    cp ${registration} etc/nix/registration

    ln -s ${pkgs.tini}/bin/tini usr/bin/tini
    ln -s ${pkgs.coreutils}/bin/env usr/bin/env
    # Clear setgid: a directory can inherit it on the runner (fsGroup), and the
    # syscall filter of Nix refuses a chmod that keeps it.
    chmod a+rwx,g-s,+t tmp
  '';
  fakeRootCommands = ''
    chown ${uid}:${uid} workspace home/sandbox
  '';
  config = {
    User = "${uid}:${uid}";
    WorkingDir = "/workspace";
    Entrypoint = [
      "/usr/bin/tini"
      "--"
    ];
    Cmd = [
      "sleep"
      "infinity"
    ];
    Env = [
      "HOME=/home/sandbox"
      "USER=sandbox"
      "PATH=/bin:/usr/bin"
      "SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
      "NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-bundle.crt"
      "BASH_ENV=/etc/sandbox/github-env.sh"
      "GIT_CONFIG_SYSTEM=/etc/gitconfig"
    ];
  };
}
// {
  inherit builder registration;
}

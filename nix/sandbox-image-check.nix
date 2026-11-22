# Reads the configuration and the files of the built sandbox image. The check
# fails when the user, the entrypoint or a tool is wrong, so a wrong image
# never reaches the registry.
{ pkgs, image }:
let
  tools = [
    "git" "gh" "ssh" "bash" "sh" "ls" "find" "grep" "sed" "awk" "rg" "fd" "jq"
    "curl" "tar" "gzip" "unzip" "which" "python3" "nix" "devenv" "direnv" "tini"
    "nix-base-build" "nix-store"
  ];
in
pkgs.runCommand "sandbox-image-check"
  { nativeBuildInputs = [ pkgs.jq pkgs.gnutar pkgs.gzip pkgs.git pkgs.bash ]; }
  ''
    fail() { echo "FAIL: $*" >&2; failed=1; }
    failed=0

    mkdir archive rootfs
    tar -xf ${image} -C archive 2>/dev/null || tar -xzf ${image} -C archive
    config=$(jq -r '.[0].Config' archive/manifest.json)
    for layer in $(jq -r '.[0].Layers[]' archive/manifest.json); do
      tar -xf "archive/$layer" -C rootfs --exclude='dev/*' 2>/dev/null || true
      chmod -R u+w rootfs
    done

    # The sticky bit does not survive an unpack by a user that is not root, so
    # read the mode of /tmp from the listing of the layers.
    for layer in $(jq -r '.[0].Layers[]' archive/manifest.json); do
      tar -tvf "archive/$layer" 2>/dev/null || true
    done > layers.txt
    grep -Eq '^drwxrwxrwt .* (\./)?tmp/?$' layers.txt || fail "/tmp is not mode 1777"

    cfg() { jq -r "$1" "archive/$config"; }

    [ "$(cfg '.config.User')" = "10000:10000" ] || fail "User is $(cfg '.config.User'), want 10000:10000"
    [ "$(cfg '.config.Entrypoint | join(" ")')" = "/usr/bin/tini --" ] || fail "Entrypoint is $(cfg '.config.Entrypoint | join(" ")')"
    [ "$(cfg '.config.Cmd | join(" ")')" = "sleep infinity" ] || fail "Cmd is $(cfg '.config.Cmd | join(" ")')"

    # A link in the image points into /nix/store, so resolve it in rootfs.
    resolve() {
      p=$1
      for _ in 1 2 3 4 5 6 7 8; do
        [ -L "rootfs$p" ] || break
        t=$(readlink "rootfs$p")
        case "$t" in /*) p=$t ;; *) p=$(dirname "$p")/$t ;; esac
      done
      [ -e "rootfs$p" ]
    }
    for tool in ${pkgs.lib.escapeShellArgs tools}; do
      resolve "/bin/$tool" || fail "tool $tool is not in /bin"
    done
    resolve /usr/bin/tini || fail "/usr/bin/tini is missing"
    resolve /etc/ssl/certs/ca-bundle.crt || fail "CA certificates are missing"

    grep -q '^[^:]*:x:10000:10000:' rootfs/etc/passwd || fail "no passwd entry for uid 10000"
    grep -q '^/nix/store/' rootfs/etc/nix/registration || fail "no store paths in /etc/nix/registration"
    grep -q '^sandbox *= *false' rootfs/etc/nix/nix.conf || fail "nix.conf has no sandbox = false"

    # The Nix cache must come first. Then a path outside the Nix base comes
    # from the cluster and not from the internet. Trust the public keys of the
    # upstream caches. ncps passes the signatures through.
    nixconf=rootfs/etc/nix/nix.conf
    first=$(sed -n 's/^substituters *= *//p' "$nixconf" | awk '{print $1}')
    [ "$first" = "http://nix-cache.system.svc.cluster.local:8501" ] \
      || fail "the first cache of nix.conf is '$first', want the Nix cache"
    keys=$(sed -n 's/^trusted-public-keys *= *//p' "$nixconf")
    for key in cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY= \
      nix-community.cachix.org-1:mB9FSh9qf2dCimDSUo8Zy7bkq5CX+/rkCWyvRCYg3Fs= \
      devenv.cachix.org-1:w1cLUi8dv3hnoSPGAuibQv+f9TZLr6cv/Hm9XgU50cw=; do
      case " $keys " in *" $key "*) ;; *) fail "trusted-public-keys has no $key" ;; esac
    done

    # The GitHub tokens (ADR 0008): the exec of a command is not a login shell, so
    # BASH_ENV must name the script, and the system file of git must be in /etc.
    [ "$(cfg '.config.Env | map(select(startswith("BASH_ENV="))) | join(" ")')" = "BASH_ENV=/etc/sandbox/github-env.sh" ] \
      || fail "Env has no BASH_ENV for the token script"
    [ "$(cfg '.config.Env | map(select(startswith("GIT_CONFIG_SYSTEM="))) | join(" ")')" = "GIT_CONFIG_SYSTEM=/etc/gitconfig" ] \
      || fail "Env has no GIT_CONFIG_SYSTEM=/etc/gitconfig"
    grep -q 'sandbox/github-env.sh' rootfs/etc/profile || fail "/etc/profile does not read the token script"
    [ -f rootfs/etc/sandbox/github-env.sh ] && [ ! -L rootfs/etc/sandbox/github-env.sh ] \
      || fail "the token script is not a real file"
    [ -f rootfs/etc/gitconfig ] && [ ! -L rootfs/etc/gitconfig ] || fail "/etc/gitconfig is not a real file"
    ! grep -Eq 'gh[pousr]_|github_pat_' rootfs/etc/gitconfig rootfs/etc/sandbox/github-env.sh \
      || fail "a token is in the image"

    # Run the script and the credential helpers of the image on fake token files.
    secrets=$PWD/secrets
    mkdir secrets home
    printf '%s\n' tok-agents > secrets/GH_TOKEN
    printf '%s\n' tok-agents > secrets/GITHUB_TOKEN
    printf '%s\n' tok-personal > secrets/GITHUB_TOKEN_PERSONAL
    exported=$(env -i BASH_ENV="$PWD/rootfs/etc/sandbox/github-env.sh" GITHUB_SECRETS_DIR="$secrets" \
      ${pkgs.bash}/bin/bash -c 'echo "$GH_TOKEN|$GITHUB_TOKEN|$GITHUB_TOKEN_PERSONAL"')
    [ "$exported" = "tok-agents|tok-agents|tok-personal" ] || fail "the token script exports: $exported"
    # A variable that the caller set is not replaced.
    kept=$(env -i BASH_ENV="$PWD/rootfs/etc/sandbox/github-env.sh" GITHUB_SECRETS_DIR="$secrets" GH_TOKEN=mine \
      ${pkgs.bash}/bin/bash -c 'echo "$GH_TOKEN|$GITHUB_TOKEN"')
    [ "$kept" = "mine|tok-agents" ] || fail "the token script replaces a variable that is set: $kept"
    # With no secret mount the script exports nothing and does not fail.
    none=$(env -i BASH_ENV="$PWD/rootfs/etc/sandbox/github-env.sh" GITHUB_SECRETS_DIR="$PWD/absent" \
      ${pkgs.bash}/bin/bash -c 'echo "[$GH_TOKEN]"')
    [ "$none" = "[]" ] || fail "the token script with no Secret prints: $none"

    sed "s|/run/secrets/github|$secrets|g" rootfs/etc/gitconfig > gitconfig
    credential() {
      printf 'protocol=https\nhost=github.com\npath=%s\n\n' "$1" |
        env -i HOME="$PWD/home" PATH="${pkgs.git}/bin:${pkgs.coreutils}/bin" GIT_CONFIG_SYSTEM="$PWD/gitconfig" \
          GIT_CONFIG_NOSYSTEM= git credential fill | sed -n 's/^password=//p'
    }
    [ "$(credential jaimeagents/repo.git)" = "tok-agents" ] || fail "git has the wrong token for jaimeagents"
    [ "$(credential jaimemachadoneto/repo.git)" = "tok-personal" ] || fail "git has the wrong token for jaimemachadoneto"
    [ "$(credential other/repo.git)" = "tok-agents" ] || fail "git has the wrong token for another owner"

    [ "$failed" = 0 ] || exit 1
    touch $out
  ''

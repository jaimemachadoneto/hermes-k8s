# Reads the configuration and the files of the built sandbox image. The check
# fails when the user, the entrypoint or a tool is wrong, so a wrong image
# never reaches the registry.
{ pkgs, image }:
let
  tools = [
    "git" "gh" "ssh" "bash" "sh" "ls" "find" "grep" "sed" "awk" "rg" "fd" "jq"
    "curl" "tar" "gzip" "unzip" "which" "python3" "nix" "devenv" "direnv" "tini"
  ];
in
pkgs.runCommand "sandbox-image-check"
  { nativeBuildInputs = [ pkgs.jq pkgs.gnutar pkgs.gzip ]; }
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
    grep -q '^sandbox *= *false' rootfs/etc/nix/nix.conf || fail "nix.conf has no sandbox = false"

    [ "$failed" = 0 ] || exit 1
    touch $out
  ''

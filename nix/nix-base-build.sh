# The builder of a Nix base (ADR 0003). The Nix base Job runs it in the sandbox image.
# `copy` copies the Nix store of the image into the volume, the same way as the init step
# of a sandbox. `clone` clones each repository of NIX_BASE_REPOS. `warm` runs `devenv shell`
# in each clone, so that the store of the volume holds what these shells need. `all` does the
# three steps in this order.
# The Job runs `copy` with the volume at /data, `clone` with the token, and then `warm` with
# the volume at /nix and with no token: the paths of the store must be the real paths while
# Nix runs, and `warm` runs code of the repositories.
#
# Variables, with their defaults:
#   NIX_BASE_TARGET        /data                          the root of the volume
#   NIX_BASE_SOURCE_STORE  /nix/store                     the store of the image
#   NIX_BASE_REGISTRATION  /etc/nix/registration          the store paths of the image
#   NIX_BASE_REPOS         (empty)                        "owner/name" words, in this order
#   NIX_BASE_GIT_URL       https://github.com             the clone URL is <url>/<repo>.git
#   NIX_BASE_TOKEN_FILE    /run/secrets/github/GITHUB_TOKEN  the token for the private repositories
#   NIX_BASE_WORK          /tmp/nix-base                  the directory for the clones
#   NIX_BASE_DEVENV        devenv                         the devenv command
set -euo pipefail

target=${NIX_BASE_TARGET:-/data}
source_store=${NIX_BASE_SOURCE_STORE:-/nix/store}
registration=${NIX_BASE_REGISTRATION:-/etc/nix/registration}
repos=${NIX_BASE_REPOS:-}
git_url=${NIX_BASE_GIT_URL:-https://github.com}
token_file=${NIX_BASE_TOKEN_FILE:-/run/secrets/github/GITHUB_TOKEN}
work=${NIX_BASE_WORK:-/tmp/nix-base}
devenv=${NIX_BASE_DEVENV:-devenv}

copy() {
  mkdir -p "$target/nix" "$target/workspace" "$target/home"
  # The marker shows that the copy is complete. The init step of a sandbox that starts from
  # this volume finds the marker and does nothing.
  if [ -e "$target/nix/.closure-copied" ]; then
    echo "nix-base-build: the volume has a complete copy already"
    return
  fi
  if [ -e "$target/nix/store" ]; then
    # The store is read-only. An incomplete copy needs chmod before rm.
    chmod -R u+w "$target/nix/store" || true
    rm -rf "$target/nix/store"
  fi
  cp -a --no-preserve=ownership "$source_store" "$target/nix/store"
  # Without its database, Nix does not know the paths of the copy and downloads them again.
  # The store of the image is read-only, and Nix writes `.links` into the store that it opens.
  # The root of the store URL sends the store, the database and the logs to the volume.
  nix-store --store "local?root=$target" --load-db <"$registration"
  touch "$target/nix/.closure-copied"
}

# The token is only for `git clone`. Git asks the helper for it when a server asks for
# credentials, so the token stays out of the arguments of each process. The `warm` step runs
# code of the repositories and gets no token, so the flake inputs of a shell must be public.
use_token() {
  if [ ! -r "$token_file" ]; then
    echo "nix-base-build: no token at $token_file, the clone has no credentials" >&2
    return
  fi
  export NIX_BASE_TOKEN_FILE=$token_file
  export GIT_TERMINAL_PROMPT=0
  export GIT_CONFIG_COUNT=1
  export GIT_CONFIG_KEY_0=credential.helper
  # shellcheck disable=SC2016 # The shell of git expands this text, not this shell.
  export GIT_CONFIG_VALUE_0='!f() { echo username=x-access-token; printf "password=%s\n" "$(cat "$NIX_BASE_TOKEN_FILE")"; }; f'
}

clone() {
  use_token
  rm -rf "$work"
  mkdir -p "$work"
  for repo in $repos; do
    echo "nix-base-build: clone $repo"
    git clone --quiet --depth 1 "$git_url/$repo.git" "$work/$(basename "$repo")"
  done
}

warm() {
  for repo in $repos; do
    echo "nix-base-build: devenv shell in $repo"
    (cd "$work/$(basename "$repo")" && "$devenv" shell -- true)
  done
  # The clones are not part of the Nix base. The evaluation cache and the store are.
  rm -rf "$work"
  sync
}

case "${1:-all}" in
copy) copy ;;
clone) clone ;;
warm) warm ;;
all)
  copy
  clone
  warm
  ;;
*)
  echo "usage: nix-base-build [copy|clone|warm|all]" >&2
  exit 2
  ;;
esac

# The builder of a Nix base (ADR 0003). The Nix base Job runs it in the sandbox image.
# `copy` copies the Nix store of the image into the volume, the same way as the init step
# of a sandbox. `warm` runs `devenv shell` for each repository in NIX_BASE_REPOS, so that the
# store of the volume holds what these shells need. `all` does both.
# The Job runs `copy` with the volume at /data, and then `warm` with the volume at /nix: the
# paths of the store must be the real paths while Nix runs.
#
# Variables, with their defaults:
#   NIX_BASE_TARGET        /data                          the root of the volume
#   NIX_BASE_SOURCE_STORE  /nix/store                     the store of the image
#   NIX_BASE_REGISTRATION  /etc/nix/registration          the store paths of the image
#   NIX_BASE_REPOS         (empty)                        "owner/name" words, in this order
#   NIX_BASE_GIT_URL       https://github.com             the clone URL is <url>/<repo>.git
#   NIX_BASE_TOKEN_FILE    /run/secrets/github/GITHUB_TOKEN  a token for private repositories
#   NIX_BASE_WORK          /tmp/nix-base                  the place of the clones
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
  # The marker shows that the copy is complete. A sandbox that starts from this volume
  # sees it, and its init step does nothing.
  if [ -e "$target/nix/.closure-copied" ]; then
    echo "nix-base-build: the volume has a complete copy already"
    return
  fi
  if [ -e "$target/nix/store" ]; then
    # The store is read-only. An incomplete copy needs chmod before rm.
    chmod -R u+w "$target/nix/store"
    rm -rf "$target/nix/store"
  fi
  cp -a --no-preserve=ownership "$source_store" "$target/nix/store"
  # Without its database, Nix does not know the paths of the copy and downloads them again.
  NIX_STATE_DIR=$target/nix/var/nix NIX_LOG_DIR=$target/nix/var/log/nix \
    nix-store --load-db <"$registration"
  touch "$target/nix/.closure-copied"
}

# Git asks the helper for the token only when a server asks for credentials. The token stays
# out of the arguments of each process.
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
  # An anonymous Nix fetch of a GitHub flake hits the rate limit of the API.
  NIX_CONFIG="${NIX_CONFIG:+$NIX_CONFIG$'\n'}access-tokens = github.com=$(cat "$token_file")"
  export NIX_CONFIG
}

warm() {
  use_token
  rm -rf "$work"
  mkdir -p "$work"
  for repo in $repos; do
    dir=$work/$(basename "$repo")
    echo "nix-base-build: clone $repo"
    git clone --quiet --depth 1 "$git_url/$repo.git" "$dir"
    echo "nix-base-build: devenv shell in $repo"
    (cd "$dir" && "$devenv" shell -- true)
  done
  # The clones are not part of the Nix base. The evaluation cache and the store are.
  rm -rf "$work"
  sync
}

case "${1:-all}" in
copy) copy ;;
warm) warm ;;
all)
  copy
  warm
  ;;
*)
  echo "usage: nix-base-build [copy|warm|all]" >&2
  exit 2
  ;;
esac

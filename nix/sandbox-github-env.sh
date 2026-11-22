# Exports the GitHub tokens from the files of the Secret of the profile. bash reads this file
# for each `bash -c` command (BASH_ENV) and for a login shell (/etc/profile). A command is not
# a login shell, so the file is the only way the tokens reach `gh`. The tokens come from files,
# so no token is in an exec request. `git` reads the same files in the credential helper of
# /etc/gitconfig.
_dir="${GITHUB_SECRETS_DIR:-/run/secrets/github}"
for _key in GH_TOKEN GITHUB_TOKEN GITHUB_TOKEN_PERSONAL; do
  # A variable that the caller set wins, so `GH_TOKEN=x make` keeps x in a nested bash.
  if [ -z "${!_key:-}" ] && [ -r "$_dir/$_key" ]; then
    IFS= read -r _value <"$_dir/$_key" || true
    export "$_key=$_value"
  fi
done
unset _dir _key _value

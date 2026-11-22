# The sandbox backend does not sync files with the Hermes home

The Hermes `FileSyncManager` copies `~/.hermes` of the host into the
sandbox every 5 seconds and copies it back at cleanup. In the Hermes pod,
that directory is `/opt/data`, which holds the `.env` of each profile and
the auth files. The plugin turns the sync off, so no secret of Hermes goes
into a sandbox, and a sandbox cannot write into the Hermes home.

## Consequences

- A skill that runs a script in the shell does not find the script in the
  sandbox. If a skill needs this, add a sync of `skills/` only, in one
  direction.

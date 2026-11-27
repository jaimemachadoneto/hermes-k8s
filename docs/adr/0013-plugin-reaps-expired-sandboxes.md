---
status: accepted
---

# The plugin archives and deletes an expired sandbox

This ADR adds to ADR 0002, which gives the end of the life of a sandbox to
`shutdownTime`.

A sandbox expires when it is suspended and its last use is more than 7 days
old. Each last use writes the annotation `hermes-k8s/last-used` and moves
`shutdownTime` to the last use plus 7 days. Each Sandbox has
`shutdownPolicy: Retain`. At `shutdownTime`, the controller deletes the pod
and keeps the Sandbox and its volume. It starts no pod while `shutdownTime` is
in the past. Thus a resume writes the last use before it sets `Running`.

The controller cannot make an archive before it deletes a Sandbox. Thus the
reaper of the plugin makes the archive and does the delete. The reaper runs
when Hermes starts and then each hour. It acts only on a Sandbox that has the
label `hermes-task`, and never on a running one.

- For each expired sandbox, the reaper makes a sandbox archive.
- The archive is a VolumeSnapshot with the labels
  `hermes-k8s/kind: sandbox-archive`, `hermes-k8s/sandbox` and
  `hermes-k8s/expires`.
- The name of the archive comes from the sandbox and its last use. Two
  reapers find the same archive. A later use of the same session gives a new
  archive.
- When the archive is ready, the reaper reads the sandbox again. It deletes
  the sandbox only when the last use did not change. The delete has the
  `resourceVersion` of that read as a precondition. A resume after the read
  changes the version, and the API server refuses the delete.
- The reaper deletes each sandbox archive after 30 days.

## Considered Options

- `shutdownPolicy: Delete`: the controller deletes the Sandbox and its volume
  with no archive.
- A CronJob in home-ops: it needs its own account and image, and it repeats
  the rules of the plugin.

## Consequences

- When Hermes is down, the controller stops the pod at `shutdownTime`. The
  reaper deletes the sandbox at its next run, never early.
- When the VolumeSnapshot quota is full, the archive fails and the sandbox
  stays until the next run.
- A failed archive is deleted, so the next run can make it again. An archive
  that is not ready stays, and the next run waits for it again.
- A suspended sandbox with no last-use time gets one at the next run, so it
  expires 7 days later.
- Each new sandbox has the annotations `hermes-k8s/task` and
  `hermes-k8s/profile`. A person can then find the session of a sandbox. The
  task annotation holds the task id of Hermes, which can contain a chat id.

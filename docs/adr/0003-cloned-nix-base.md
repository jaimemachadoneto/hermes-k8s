# /nix of a sandbox is a copy-on-write clone of a Nix base

Each sandbox volume is a Ceph RBD clone of a VolumeSnapshot, the Nix base.
The volume holds `/nix` and the workspace. A clone shares the blocks of the
Nix base, so a sandbox stores only its own changes and starts with the
common packages already present.

## Consequences

- The volume at `/nix` hides the Nix store of the image. Thus each Nix base
  contains the closure of one sandbox image, and the plugin selects the Nix
  base by the image digest. A Job makes a new Nix base for each new sandbox
  image and each week.
- Each Nix base starts from an empty volume, never from a clone, so each
  clone chain has one level.
- Nix runs as a single user, uid 10000, with `sandbox = false` and no
  daemon.
- Packages that one sandbox builds are not shared with other sandboxes. The
  Nix cache in the cluster closes most of that gap.

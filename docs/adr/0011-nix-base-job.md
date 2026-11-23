# A Job in home-ops builds each Nix base, and the plugin selects and deletes it

ADR 0003 says that a Job makes the Nix base. This ADR gives the parts of that Job.
The pod of the Job has an ephemeral volume of 20Gi and three steps. The init step
`copy-closure` runs `nix-base-build copy` with the volume at `/data`. The init step `warm`
runs `nix-base-build warm` with the volume at `/nix`, so that the shell of each configured
repository (`devenv shell`, or `nix develop` for a repository with only a flake) runs on the
real paths of the store. The container `snapshot` makes the
VolumeSnapshot (class `csi-ceph-blockpool`) with the labels `hermes-k8s/kind=nix-base` and
`hermes-k8s/image-digest=<first 32 characters of the digest>`. It then deletes the volume to
free the quota. The plugin selects the newest ready Nix base that has the label of the digest of the sandbox image. If
no Nix base fits, the volume of the sandbox is empty. When it starts an environment, the plugin deletes the ready Nix
bases of the current image except the two newest. It also deletes a Nix base of the current image
that is not ready after 24 hours. When the current image has a ready Nix base, the plugin deletes every ready Nix base of the
other images. This keeps the VolumeSnapshot quota free for the sandbox archives. The user made this
decision on 2026-11-22. Before that date, the plugin kept two Nix bases of each image. Until the
current image has a ready Nix base, the plugin keeps the Nix bases of the other images. The plugin
does not delete a Nix base that has no digest label.

## Considered Options

- The manifests in this repository, and a Flux source for it in home-ops: the repository is
  private, so Flux needs one more credential. The digest of the image is in home-ops, and
  Renovate changes it there. The manifests are in home-ops.
- The GitHub Secret of a profile (`hermes-sandbox-<profile>`, ADR 0008) for the clone: the Job
  belongs to no profile, and the Secret has the tokens that can push. The Job has its own
  Secret `hermes-nix-base-github`. A sandbox cannot mount it.
- A snapshot step that runs the same container as the build: the build step runs code of
  the repositories, so it has no API token. Only the `snapshot` container has the token.

## Consequences

- The sandbox image has the list of its store paths at `/etc/nix/registration`. The init step
  of a sandbox and `nix-base-build copy` load it into the Nix database of the volume. Without
  it, Nix does not know the paths of the copy and downloads them again.
- The Job has the image digest in `nix-base.yaml`, not in the HelmRelease. The digest of the Job must be
  the digest in the HelmRelease of Hermes, or the plugin finds no Nix base for its image.
  The release workflow sets both in one deploy PR (`scripts/set-image-refs.py` with
  `--nix-base-file`). It fails when `nix-base.yaml` has no sandbox image line.
- The Nix base must fit in 20Gi, the maximum size of a sandbox volume in the admission policy.
- Flux replaces the Job when the image changes (`kustomize.toolkit.fluxcd.io/force`), and
  the CronJob runs it each week.
- A clone can hold a Nix base. If the CSI driver refuses to delete a snapshot that has a
  clone, the snapshot stays until the clone goes. The next start of an environment tries
  again.
- The plugin cannot tell an older image from a newer image. If Hermes stays on the old image
  after a new Nix base is ready, for example after a failed upgrade, the plugin deletes the new Nix
  base. The next CronJob run makes it again.

# The sandboxes use the pod network with Cilium, not VLAN 70

A sandbox gets DNS, the internet without the private ranges, the Nix cache
and SearXNG from a base CiliumNetworkPolicy. Each other service has its own
policy in home-ops, which selects a label. The plugin sets that label from
the service allowances of the profile. A sandbox gets no ingress.

## Considered Options

- A Multus macvlan interface on VLAN 70, the zone of the Paperclip VM. The
  firewall would then control the sandboxes, but the nodes need VLAN 70 on
  their trunk, and the macvlan traffic bypasses Cilium. This can come later
  as a second layer.

"""Service allowances (ADR 0007): the labels that let the sandboxes of a profile reach a service.

The setting `service_allowances` of the plugin maps a profile name to a list of services. Each
service becomes the label `hermes-k8s/allow-<service>=true` on the pod of the sandbox. A
CiliumNetworkPolicy of home-ops selects that label.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

LABEL_PREFIX = "hermes-k8s/allow-"
SETTING = "service_allowances"

# The name part of a label key has a maximum of 63 characters. The text `allow-` uses 6 of them,
# so a service name has a maximum of 57 characters.
_SERVICE = re.compile(r"[a-z0-9]([a-z0-9-]{0,55}[a-z0-9])?")


def allowance_labels(services: Iterable[str]) -> dict[str, str]:
    """Return one label for each service. Raise ValueError for a name that is not valid in a label."""
    labels = {}
    for service in services:
        if not _SERVICE.fullmatch(service):
            raise ValueError(f"the service name {service!r} is not valid in a label key")
        labels[f"{LABEL_PREFIX}{service}"] = "true"
    return labels


def services_for_profile(setting: object, profile: str) -> list[str]:
    """Return the services that the setting gives to `profile`.

    No setting, or no entry for the profile, gives no service. An entry that is not a list of
    strings is an error of the operator. Raise ValueError. Do not guess.
    """
    if not isinstance(setting, Mapping) or profile not in setting:
        return []
    services = setting[profile]
    if services is None:
        return []
    if not isinstance(services, list) or not all(isinstance(s, str) for s in services):
        raise ValueError(f"{SETTING} of the profile {profile!r} must be a list of service names")
    return list(services)

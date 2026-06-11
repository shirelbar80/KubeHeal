"""Safety guardrails for proposed patches (Phase 2).

Enforces the patch allow-list: ONLY ``resources`` (limits/requests) and probes
(liveness/readiness/startup) may be mutated. Anything touching image registries,
securityContext escalation, hostPath volumes, replicas, etc. is rejected before
it can reach the cluster.

TODO(Phase 2): implement allow-list validation over the strategic-merge patch.
"""

from __future__ import annotations

from typing import Any

# Top-level container fields KubeHeal is permitted to modify.
ALLOWED_FIELDS: set[str] = {
    "resources",        # limits / requests
    "livenessProbe",
    "readinessProbe",
    "startupProbe",
}


def validate_patch(patch: dict[str, Any]) -> None:  # pragma: no cover - stub
    """Raise if the patch touches anything outside ALLOWED_FIELDS."""
    raise NotImplementedError("Safety validation is implemented in Phase 2.")

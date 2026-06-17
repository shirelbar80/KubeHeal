"""Deterministic fix for a slow-starting container killed by its liveness probe.

When a liveness/readiness probe is failing but its port already matches a declared
containerPort, the probe target is correct — the app is simply too slow to start.
The robust, model-independent fix is to add a ``startupProbe`` mirroring the
probe with a generous failure budget, so the liveness probe only begins once the
app is up. (A small local LLM is unreliable at choosing this fix, so we compute
it directly and route it through the normal approve → dry-run → verify flow.)
"""

from __future__ import annotations

from typing import Any

from .logging_setup import get_logger, kv
from .models import Diagnosis, Incident
from .safety import UnsafePatchError, validate_patch

log = get_logger("kubeheal.probefix")

_STARTUP_PERIOD = 5       # seconds between startup checks
_STARTUP_FAILURES = 30    # 5s * 30 = 150s startup budget before giving up


def _probe_handler(probe: Any) -> tuple[str, dict] | None:
    if not isinstance(probe, dict):
        return None
    for name in ("httpGet", "tcpSocket", "grpc"):
        if isinstance(probe.get(name), dict):
            return name, probe[name]
    return None


def assess(incident: Incident) -> Diagnosis | None:
    """Return a deterministic startupProbe Diagnosis for a slow-start crashloop,
    or None if this isn't a clean slow-start (let the LLM handle those)."""
    events = incident.events.lower()
    if not ("liveness probe" in events or "readiness probe" in events):
        return None

    spec = incident.current_spec
    if spec.get("startup_probe"):
        return None  # already has a startupProbe — not the missing-grace case

    probe = spec.get("liveness_probe") or spec.get("readiness_probe")
    handler = _probe_handler(probe)
    if handler is None:
        return None
    hname, hval = handler

    port = hval.get("port")
    container_ports = {p.get("containerPort") for p in (spec.get("ports") or []) if isinstance(p, dict)}
    if port is None or port not in container_ports:
        return None  # wrong/unknown port — not a clean slow-start; the LLM can try

    startup_probe = {hname: hval, "periodSeconds": _STARTUP_PERIOD, "failureThreshold": _STARTUP_FAILURES}
    patch = {"spec": {"template": {"spec": {"containers": [
        {"name": incident.container_name, "startupProbe": startup_probe}
    ]}}}}

    try:
        validate_patch(patch)
    except UnsafePatchError:  # pragma: no cover - defensive; we build a valid patch
        return None

    budget = _STARTUP_PERIOD * _STARTUP_FAILURES
    log.info("slow-start detected %s", kv(workload=incident.workload_name, port=port))
    return Diagnosis(
        diagnosis=(f"The '{incident.container_name}' container is killed by its liveness probe "
                   f"during a slow startup (the probe port {port} is correct)."),
        root_cause=("The liveness probe has no startup grace period, so it restarts the container "
                    "before the app finishes starting."),
        confidence=0.9,
        patch=patch,
        patch_explanation=(f"Add a startupProbe (same check, up to {budget}s) so the liveness probe "
                           "only begins once the app has started."),
    )

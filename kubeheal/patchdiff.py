"""Render a proposed patch as a human-readable before → after diff.

The Slack approval shows e.g. ``resources.limits.memory: 10Mi → 40Mi`` instead of
a raw JSON blob, so a reviewer can see at a glance what will change. Works for any
allow-listed patch (resources + probes) by flattening the current and proposed
values to dotted leaf paths and reporting the differences.
"""

from __future__ import annotations

from typing import Any

# camelCase patch field -> snake_case key in Incident.current_spec.
_FIELD_MAP = {
    "resources": "resources",
    "livenessProbe": "liveness_probe",
    "readinessProbe": "readiness_probe",
    "startupProbe": "startup_probe",
}


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a nested dict to {dotted.path: scalar}. Lists/scalars are leaves."""
    if not isinstance(obj, dict):
        return {prefix: obj}
    flat: dict[str, Any] = {}
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, path))
        else:
            flat[path] = value
    return flat


def render_diff(current_spec: dict[str, Any], patch: dict[str, Any]) -> list[str]:
    """Return ``field.path: old → new`` lines for each leaf the patch changes."""
    try:
        container = patch["spec"]["template"]["spec"]["containers"][0]
    except (KeyError, IndexError, TypeError):
        return []

    lines: list[str] = []
    for field, proposed in container.items():
        if field == "name":
            continue
        current = current_spec.get(_FIELD_MAP.get(field, field))
        cur_flat = _flatten(current) if isinstance(current, dict) else {}
        new_flat = _flatten(proposed) if isinstance(proposed, dict) else {field: proposed}
        for path, new_val in new_flat.items():
            old_val = cur_flat.get(path)
            if str(old_val) != str(new_val):
                old_disp = "(none)" if old_val is None else old_val
                lines.append(f"{field}.{path}: {old_disp} → {new_val}")
    return lines

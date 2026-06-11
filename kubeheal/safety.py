"""Safety guardrails for proposed patches.

Enforces the patch allow-list: the LLM may ONLY mutate a container's
``resources`` and probes, and only via a strategic-merge patch shaped as
``spec.template.spec.containers[*]``. Anything else — image, command, env,
securityContext, volumes, replicas, extra top-level keys — is rejected before
the patch can reach the cluster. LLM output is never trusted blindly.
"""

from __future__ import annotations

from typing import Any

# Container fields KubeHeal is permitted to modify (plus the required "name").
ALLOWED_FIELDS: set[str] = {
    "resources",
    "livenessProbe",
    "readinessProbe",
    "startupProbe",
}

# Only cpu/memory may appear under resources.limits / resources.requests.
_ALLOWED_RESOURCE_KEYS: set[str] = {"cpu", "memory"}


class UnsafePatchError(ValueError):
    """Raised when a proposed patch touches anything outside the allow-list."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise UnsafePatchError(message)


def _validate_resources(resources: dict[str, Any]) -> None:
    _require(isinstance(resources, dict), "resources must be an object")
    for section, value in resources.items():
        _require(
            section in {"limits", "requests"},
            f"resources.{section} is not allowed (only limits/requests)",
        )
        _require(isinstance(value, dict), f"resources.{section} must be an object")
        for key in value:
            _require(
                key in _ALLOWED_RESOURCE_KEYS,
                f"resources.{section}.{key} is not allowed (only cpu/memory)",
            )


def _validate_container(container: dict[str, Any]) -> None:
    _require(isinstance(container, dict), "each container must be an object")
    _require("name" in container and container["name"], "container patch must include 'name'")
    for field in container:
        if field == "name":
            continue
        _require(
            field in ALLOWED_FIELDS,
            f"container field '{field}' is not allowed "
            f"(only {sorted(ALLOWED_FIELDS)} may be modified)",
        )
    if "resources" in container:
        _validate_resources(container["resources"])


def validate_patch(patch: dict[str, Any]) -> None:
    """Raise ``UnsafePatchError`` unless ``patch`` only touches allowed fields.

    Accepts exactly: {"spec": {"template": {"spec": {"containers": [ {...} ]}}}}
    """
    _require(isinstance(patch, dict), "patch must be an object")
    _require(set(patch) == {"spec"}, "patch may only contain a top-level 'spec'")

    spec = patch["spec"]
    _require(isinstance(spec, dict) and set(spec) == {"template"}, "spec may only contain 'template'")

    template = spec["template"]
    _require(
        isinstance(template, dict) and set(template) == {"spec"},
        "spec.template may only contain 'spec'",
    )

    tspec = template["spec"]
    _require(
        isinstance(tspec, dict) and set(tspec) == {"containers"},
        "spec.template.spec may only contain 'containers'",
    )

    containers = tspec["containers"]
    _require(
        isinstance(containers, list) and len(containers) >= 1,
        "containers must be a non-empty list",
    )
    for container in containers:
        _validate_container(container)

"""Detect a failing *recent deploy* and propose reverting to the prior revision.

This is a deterministic remediation (no LLM): when a Deployment's newest revision
is failing and the *previous* revision had a meaningfully different spec, the
safest fix is to roll back — return to a template that already ran. Two guards
keep it from firing spuriously:

  1. Recency — the current revision must have been created within a window, so we
     only act when the recent deploy is plausibly the cause.
  2. Substantive change — the current template must differ from the previous one
     in image/command/args/env. This ignores ``rollout restart`` and scaling,
     which create new revisions with an otherwise-identical template.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kubernetes.client.exceptions import ApiException

from .k8s import apps_v1
from .logging_setup import get_logger

log = get_logger("kubeheal.rollback")

_REVISION_ANN = "deployment.kubernetes.io/revision"
_POD_TEMPLATE_HASH = "pod-template-hash"


@dataclass
class RolloutInfo:
    from_revision: int
    to_revision: int
    to_rs_name: str
    summary: str   # human-readable description of the change being reverted


def _revision(rs: Any) -> int | None:
    ann = (rs.metadata.annotations or {}).get(_REVISION_ANN)
    try:
        return int(ann) if ann is not None else None
    except (TypeError, ValueError):
        return None


def _pick_target(replicasets: list[Any]) -> tuple[Any, Any] | None:
    """Return (current_rs, previous_rs) by revision, or None if < 2 revisions."""
    revs = [(rs, _revision(rs)) for rs in replicasets]
    revs = [(rs, n) for rs, n in revs if n is not None]
    if len(revs) < 2:
        return None
    revs.sort(key=lambda t: t[1])
    return revs[-1][0], revs[-2][0]


def _container_sig(template: Any) -> dict[str, tuple]:
    """The substantive deploy fields (image/command/args/env) per container."""
    sig: dict[str, tuple] = {}
    for c in (template.spec.containers or []):
        env = tuple((e.name, getattr(e, "value", None)) for e in (c.env or []))
        sig[c.name] = (c.image, tuple(c.command or []), tuple(c.args or []), env)
    return sig


def _substantive_change(current_rs: Any, previous_rs: Any) -> bool:
    return _container_sig(current_rs.spec.template) != _container_sig(previous_rs.spec.template)


def _first_image(rs: Any) -> str:
    containers = rs.spec.template.spec.containers or []
    return containers[0].image if containers else "?"


def assess(workload_name: str, namespace: str, window_seconds: int) -> RolloutInfo | None:
    """Return rollback info if this workload is a failing recent deploy, else None."""
    try:
        dep = apps_v1().read_namespaced_deployment(workload_name, namespace)
        selector = dep.spec.selector.match_labels or {}
        label_selector = ",".join(f"{k}={v}" for k, v in selector.items())
        replicasets = apps_v1().list_namespaced_replica_set(
            namespace, label_selector=label_selector
        ).items
    except ApiException:
        return None

    picked = _pick_target(replicasets)
    if picked is None:
        return None
    current, previous = picked

    # Guard 1: the current revision must be recent.
    created = current.metadata.creation_timestamp
    if created is not None and (datetime.now(timezone.utc) - created).total_seconds() > window_seconds:
        return None

    # Guard 2: it must be a real spec change, not a restart/scale.
    if not _substantive_change(current, previous):
        return None

    cur_img, prev_img = _first_image(current), _first_image(previous)
    summary = f"image `{cur_img}` → `{prev_img}`" if cur_img != prev_img else "pod spec change"
    return RolloutInfo(
        from_revision=_revision(current) or 0,
        to_revision=_revision(previous) or 0,
        to_rs_name=previous.metadata.name,
        summary=summary,
    )


def target_template(namespace: str, to_rs_name: str) -> Any:
    """The previous revision's pod template, cleaned of the RS-only
    ``pod-template-hash`` label, ready to set on the Deployment."""
    rs = apps_v1().read_namespaced_replica_set(to_rs_name, namespace)
    template = rs.spec.template
    if template.metadata and template.metadata.labels:
        template.metadata.labels.pop(_POD_TEMPLATE_HASH, None)
    return template

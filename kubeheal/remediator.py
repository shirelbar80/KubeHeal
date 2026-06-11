"""The Remediator — applies an approved patch safely.

Flow: capture rollback spec -> server-side dry-run -> real apply -> verify
rollout -> report. If verification fails, roll back to the captured spec.

Only Deployments are patched, with a strategic-merge patch. The patch shape is
already constrained by ``safety.validate_patch`` before it reaches here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from kubernetes.client.exceptions import ApiException

from config import settings

from .k8s import apps_v1, core_v1
from .safety import ALLOWED_FIELDS

_STRATEGIC_MERGE = "application/strategic-merge-patch+json"


@dataclass
class RemediationResult:
    ok: bool
    message: str
    rolled_back: bool = False
    dry_run_failed: bool = False


def _sanitize(obj: Any) -> Any:
    return core_v1().api_client.sanitize_for_serialization(obj)


def _capture_rollback(workload_name: str, namespace: str, container_name: str) -> dict[str, Any]:
    """Build a patch that restores the container's current allowed fields."""
    dep = apps_v1().read_namespaced_deployment(workload_name, namespace)
    container = next(
        (c for c in dep.spec.template.spec.containers if c.name == container_name), None
    )
    restore: dict[str, Any] = {"name": container_name}
    if container is not None:
        # Map snake_case client attrs to their camelCase patch keys.
        for field in ALLOWED_FIELDS:
            attr = "".join(("_" + ch.lower() if ch.isupper() else ch) for ch in field)
            value = getattr(container, attr, None)
            restore[field] = _sanitize(value) if value is not None else None
    return {"spec": {"template": {"spec": {"containers": [restore]}}}}


def _patch(workload_name: str, namespace: str, body: dict[str, Any], dry_run: bool) -> None:
    kwargs: dict[str, Any] = {"_content_type": _STRATEGIC_MERGE}
    if dry_run:
        kwargs["dry_run"] = "All"
    apps_v1().patch_namespaced_deployment(workload_name, namespace, body, **kwargs)


def _verify_rollout(workload_name: str, namespace: str, timeout: int) -> bool:
    """True if the Deployment fully rolls out (all replicas updated & ready)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            dep = apps_v1().read_namespaced_deployment(workload_name, namespace)
        except ApiException:
            time.sleep(3)
            continue
        spec_replicas = dep.spec.replicas or 0
        st = dep.status
        ready = st.ready_replicas or 0
        updated = st.updated_replicas or 0
        available = st.available_replicas or 0
        if spec_replicas and ready == updated == available == spec_replicas:
            return True
        time.sleep(3)
    return False


def apply_patch(
    workload_name: str,
    namespace: str,
    container_name: str,
    patch: dict[str, Any],
) -> RemediationResult:
    """Dry-run, apply, verify, and roll back on failure."""
    # 1. Capture current state so we can roll back.
    try:
        rollback_patch = _capture_rollback(workload_name, namespace, container_name)
    except ApiException as exc:
        return RemediationResult(ok=False, message=f"could not read deployment: {exc.reason}")

    # 2. Server-side dry-run first.
    try:
        _patch(workload_name, namespace, patch, dry_run=True)
    except ApiException as exc:
        return RemediationResult(
            ok=False, message=f"dry-run rejected the patch: {exc.reason}", dry_run_failed=True
        )

    # 3. Apply for real.
    try:
        _patch(workload_name, namespace, patch, dry_run=False)
    except ApiException as exc:
        return RemediationResult(ok=False, message=f"apply failed: {exc.reason}")

    # 4. Verify the rollout recovers.
    if _verify_rollout(workload_name, namespace, settings.verify_timeout_seconds):
        return RemediationResult(ok=True, message="patch applied; workload healthy")

    # 5. Recovery failed -> roll back.
    try:
        _patch(workload_name, namespace, rollback_patch, dry_run=False)
        return RemediationResult(
            ok=False,
            message="patch did not heal the workload; rolled back to previous spec",
            rolled_back=True,
        )
    except ApiException as exc:
        return RemediationResult(
            ok=False, message=f"patch failed AND rollback failed: {exc.reason}"
        )

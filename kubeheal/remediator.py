"""The Remediator — applies an approved patch safely.

Flow: capture rollback spec -> server-side dry-run -> real apply -> verify
rollout -> report. If verification fails, roll back to the captured spec.

Only Deployments are patched, with a strategic-merge patch. The patch shape is
already constrained by ``safety.validate_patch`` before it reaches here.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kubernetes.client.exceptions import ApiException

from config import settings

from .k8s import apps_v1, core_v1
from .logging_setup import get_logger, kv
from .safety import ALLOWED_FIELDS

_STRATEGIC_MERGE = "application/strategic-merge-patch+json"
log = get_logger("kubeheal.remediator")


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


def _annotate_applied(workload_name: str, namespace: str, message: str) -> None:
    """Record on the Deployment that KubeHeal remediated it (metadata only, so it
    does not trigger another rollout). Best-effort."""
    body = {
        "metadata": {
            "annotations": {
                "kubeheal.io/last-remediation": datetime.now(timezone.utc).isoformat(),
                "kubeheal.io/last-message": message[:200],
            }
        }
    }
    try:
        _patch(workload_name, namespace, body, dry_run=False)
    except ApiException as exc:
        log.warning("annotate failed %s", kv(workload=workload_name, error=getattr(exc, "reason", exc)))


_STABLE_POLLS = 3      # consecutive healthy readings required
_POLL_INTERVAL = 3     # seconds between readings


def _verify_rollout(workload_name: str, namespace: str, timeout: int) -> bool:
    """True only if the Deployment is fully rolled out AND stays healthy for
    several consecutive polls.

    A single poll is not enough: a crash-looping container with no readiness
    probe is briefly "ready" the instant it starts, so one reading can catch a
    transient all-ready blip. We require sustained health and a rollout whose
    status the controller has actually observed.
    """
    deadline = time.time() + timeout
    good = 0
    while time.time() < deadline:
        try:
            dep = apps_v1().read_namespaced_deployment(workload_name, namespace)
        except ApiException:
            good = 0
            time.sleep(_POLL_INTERVAL)
            continue

        spec_replicas = dep.spec.replicas or 0
        st = dep.status
        ready = st.ready_replicas or 0
        updated = st.updated_replicas or 0
        available = st.available_replicas or 0
        observed = st.observed_generation or 0
        generation = dep.metadata.generation or 0

        healthy = (
            spec_replicas > 0
            and observed >= generation
            and ready == updated == available == spec_replicas
        )
        good = good + 1 if healthy else 0
        if good >= _STABLE_POLLS:
            return True
        time.sleep(_POLL_INTERVAL)
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
        _annotate_applied(workload_name, namespace, "patch applied; workload healthy")
        log.info("healed %s", kv(workload=workload_name))
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


def rollback(workload_name: str, namespace: str, to_rs_name: str) -> RemediationResult:
    """Revert a Deployment to a previous revision's pod template (like
    ``kubectl rollout undo``): dry-run, replace, verify. Bypasses the patch
    allow-list intentionally — we're restoring a template that already ran, not
    applying an LLM-proposed change."""
    from .rollback import target_template

    try:
        template = target_template(namespace, to_rs_name)
        dep = apps_v1().read_namespaced_deployment(workload_name, namespace)
    except ApiException as exc:
        return RemediationResult(ok=False, message=f"could not read deployment/replicaset: {exc.reason}")

    dep.spec.template = template

    try:
        apps_v1().replace_namespaced_deployment(workload_name, namespace, dep, dry_run="All")
    except ApiException as exc:
        return RemediationResult(
            ok=False, message=f"dry-run rejected the rollback: {exc.reason}", dry_run_failed=True
        )

    try:
        apps_v1().replace_namespaced_deployment(workload_name, namespace, dep)
    except ApiException as exc:
        return RemediationResult(ok=False, message=f"rollback apply failed: {exc.reason}")

    if _verify_rollout(workload_name, namespace, settings.verify_timeout_seconds):
        _annotate_applied(workload_name, namespace, f"rolled back to a prior revision ({to_rs_name})")
        log.info("rolled back %s", kv(workload=workload_name, to_rs=to_rs_name))
        return RemediationResult(ok=True, message="rolled back to the previous revision; workload healthy")

    return RemediationResult(
        ok=False, message="rollback applied but the workload is still not healthy — needs investigation"
    )

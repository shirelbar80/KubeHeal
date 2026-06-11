"""The Observer — event-driven failure detection via the K8s Watch API.

Watches Pod objects in the configured namespace, inspects container statuses to
detect CrashLoopBackOff / OOMKilled, resolves the owning Deployment, applies a
per-workload cooldown to prevent storms, assembles an ``Incident`` (logs +
current spec), and hands it to a callback.

Run standalone to verify detection:
    py -m kubeheal.observer
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from kubernetes import watch
from kubernetes.client.exceptions import ApiException

from config import settings

from .k8s import apps_v1, core_v1
from .log_fetcher import fetch_logs
from .models import FailureReason, Incident

# Relevant container-spec fields we surface to the Brain (matches the safety
# allow-list: resources + probes).
_RELEVANT_SPEC_FIELDS = ("resources", "liveness_probe", "readiness_probe", "startup_probe")

IncidentHandler = Callable[[Incident], None]


def _classify(status: Any) -> tuple[FailureReason, str] | None:
    """Map a V1ContainerStatus to a failure reason, or None if healthy."""
    state = status.state
    last = status.last_state

    if state and state.waiting and state.waiting.reason == "CrashLoopBackOff":
        # The crash detail lives in the previous termination.
        if last and last.terminated and last.terminated.reason == "OOMKilled":
            return FailureReason.OOM_KILLED, status.name
        return FailureReason.CRASH_LOOP_BACKOFF, status.name

    if state and state.terminated and state.terminated.reason == "OOMKilled":
        return FailureReason.OOM_KILLED, status.name

    if last and last.terminated and last.terminated.reason == "OOMKilled":
        return FailureReason.OOM_KILLED, status.name

    return None


def _resolve_workload(pod: Any) -> tuple[str, str] | None:
    """Walk ownerReferences: Pod -> ReplicaSet -> Deployment.

    Returns (kind, name) of the Deployment, or None if it can't be resolved.
    """
    refs = pod.metadata.owner_references or []
    rs_ref = next((r for r in refs if r.kind == "ReplicaSet"), None)
    if not rs_ref:
        return None
    try:
        rs = apps_v1().read_namespaced_replica_set(rs_ref.name, pod.metadata.namespace)
    except ApiException:
        return None
    dep_ref = next((r for r in (rs.metadata.owner_references or []) if r.kind == "Deployment"), None)
    if not dep_ref:
        return None
    return "Deployment", dep_ref.name


def _current_spec(workload_name: str, namespace: str, container_name: str) -> dict[str, Any]:
    """Pull the failing container's resources + probes from the Deployment."""
    try:
        dep = apps_v1().read_namespaced_deployment(workload_name, namespace)
    except ApiException:
        return {}
    containers = dep.spec.template.spec.containers or []
    container = next((c for c in containers if c.name == container_name), None)
    if container is None:
        return {}
    spec: dict[str, Any] = {"name": container.name}
    for field in _RELEVANT_SPEC_FIELDS:
        value = getattr(container, field, None)
        if value is not None:
            spec[field] = client_to_dict(value)
    return spec


def client_to_dict(obj: Any) -> Any:
    """Best-effort conversion of a K8s client model to a plain dict."""
    api = core_v1().api_client
    return api.sanitize_for_serialization(obj)


def _build_incident(pod: Any, reason: FailureReason, container_name: str) -> Incident | None:
    workload = _resolve_workload(pod)
    if workload is None:
        return None
    kind, name = workload
    logs = fetch_logs(
        pod.metadata.name,
        pod.metadata.namespace,
        container=container_name,
        tail_lines=settings.log_tail_lines,
    )
    return Incident(
        pod_name=pod.metadata.name,
        namespace=pod.metadata.namespace,
        workload_kind=kind,
        workload_name=name,
        reason=reason,
        container_name=container_name,
        logs=logs,
        current_spec=_current_spec(name, pod.metadata.namespace, container_name),
    )


def run(on_incident: IncidentHandler) -> None:
    """Watch the namespace forever, invoking ``on_incident`` per fresh failure.

    Reconnects automatically when the watch stream times out.
    """
    api = core_v1()
    ns = settings.namespace
    cooldown = settings.cooldown_seconds
    last_seen: dict[str, float] = {}

    print(f"[observer] watching namespace '{ns}' (cooldown={cooldown}s)")
    while True:
        w = watch.Watch()
        try:
            for event in w.stream(api.list_namespaced_pod, namespace=ns, timeout_seconds=60):
                pod = event["object"]
                statuses = (pod.status.container_statuses or []) if pod.status else []
                for status in statuses:
                    hit = _classify(status)
                    if hit is None:
                        continue
                    reason, container_name = hit

                    incident = _build_incident(pod, reason, container_name)
                    if incident is None:
                        continue

                    key = f"{incident.workload_kind}/{incident.workload_name}"
                    now = time.monotonic()
                    if key in last_seen and (now - last_seen[key]) < cooldown:
                        continue  # within cooldown — suppress storm
                    last_seen[key] = now

                    on_incident(incident)
        except ApiException as exc:
            print(f"[observer] watch error: {exc}; reconnecting...")
            time.sleep(2)
        finally:
            w.stop()


def _print_incident(incident: Incident) -> None:
    print("\n=== INCIDENT ===")
    print(f"  workload : {incident.workload_kind}/{incident.workload_name}")
    print(f"  pod      : {incident.pod_name}")
    print(f"  reason   : {incident.reason.value}")
    print(f"  container: {incident.container_name}")
    print(f"  spec     : {incident.current_spec}")
    print(f"  logs     :\n{incident.logs.rstrip()}")
    print("================\n")


if __name__ == "__main__":
    run(_print_incident)

"""The Observer — event-driven failure detection via the K8s Watch API.

Watches Pod objects in the configured namespace, inspects container statuses to
detect CrashLoopBackOff / OOMKilled / ImagePullBackOff / CreateContainerConfigError,
resolves the owning Deployment, applies a per-workload cooldown to prevent storms,
assembles an ``Incident`` (logs + events + current spec), and hands it to a callback.

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

from . import store
from .k8s import apps_v1, core_v1
from .log_fetcher import fetch_events, fetch_logs
from .logging_setup import get_logger, kv
from .models import FailureReason, Incident

log = get_logger("kubeheal.observer")

# Container-spec fields we surface to the Brain. resources + probes are the
# patchable ones (safety allow-list); ports is read-only context that lets the
# model spot a probe pointing at the wrong port.
_RELEVANT_SPEC_FIELDS = (
    "resources",
    "liveness_probe",
    "readiness_probe",
    "startup_probe",
    "ports",
)

IncidentHandler = Callable[[Incident], None]


def _classify(status: Any) -> tuple[FailureReason, str] | None:
    """Map a V1ContainerStatus to a failure reason, or None if healthy."""
    state = status.state
    last = status.last_state

    waiting_reason = state.waiting.reason if (state and state.waiting) else None

    if waiting_reason == "CrashLoopBackOff":
        # The crash detail lives in the previous termination.
        if last and last.terminated and last.terminated.reason == "OOMKilled":
            return FailureReason.OOM_KILLED, status.name
        return FailureReason.CRASH_LOOP_BACKOFF, status.name

    # Container never started: bad image, or a missing ConfigMap/Secret.
    if waiting_reason in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName"):
        return FailureReason.IMAGE_PULL_BACKOFF, status.name

    if waiting_reason in ("CreateContainerConfigError", "CreateContainerError"):
        return FailureReason.CONFIG_ERROR, status.name

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
    events = fetch_events(pod.metadata.name, pod.metadata.namespace)
    return Incident(
        pod_name=pod.metadata.name,
        namespace=pod.metadata.namespace,
        workload_kind=kind,
        workload_name=name,
        reason=reason,
        container_name=container_name,
        logs=logs,
        events=events,
        current_spec=_current_spec(name, pod.metadata.namespace, container_name),
    )


def run(on_incident: IncidentHandler) -> None:
    """Watch the namespace forever, invoking ``on_incident`` per fresh failure.

    Reconnects automatically when the watch stream times out.
    """
    api = core_v1()
    ns = settings.namespace
    cooldown = settings.cooldown_seconds
    store.init_db()

    log.info("watching %s", kv(namespace=ns, cooldown=cooldown))
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
                    # One open incident per workload: if an alert (approval OR a
                    # needs-a-human notice) for this workload is still unresolved,
                    # don't post a duplicate — the reminder loop nudges it instead.
                    if store.has_open_incident(
                        incident.namespace, incident.workload_kind, incident.workload_name
                    ):
                        continue
                    # SQLite-backed cooldown survives restarts and prevents storms.
                    if not store.should_alert(key, cooldown):
                        continue

                    log.info("incident %s", kv(workload=key, reason=reason.value, pod=incident.pod_name))
                    on_incident(incident)
        except ApiException as exc:
            log.warning("watch error %s", kv(error=getattr(exc, "reason", exc)))
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
    print(f"  events   :\n{incident.events.rstrip() or '(none)'}")
    print(f"  logs     :\n{incident.logs.rstrip()}")
    print("================\n")


if __name__ == "__main__":
    run(_print_incident)

"""The Observer — event-driven failure detection via the K8s Watch API.

Phase 1. Watches Pod objects in the configured namespace and inspects
``container_statuses`` to detect CrashLoopBackOff / OOMKilled, resolves the
owning workload, applies dedup + cooldown, and emits an ``Incident``.

TODO(Phase 1): implement watch loop, failure detection, owner resolution,
dedup/cooldown.
"""

from __future__ import annotations


def run() -> None:  # pragma: no cover - stub
    raise NotImplementedError("Observer is implemented in Phase 1.")

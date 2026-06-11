"""Persistence — SQLite-backed approval state + audit log (Phase 3).

Survives process restarts so a pending approval is never lost or double-applied.

Tables:
  pending_approvals(id, workload, namespace, patch_json, status, created_at)
  audit_log(id, workload, event, detail_json, actor, created_at)

TODO(Phase 3): implement schema init + CRUD for pending approvals and audit log.
"""

from __future__ import annotations


def init_db() -> None:  # pragma: no cover - stub
    raise NotImplementedError("Store is implemented in Phase 3.")

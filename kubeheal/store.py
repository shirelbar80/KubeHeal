"""Persistence — SQLite-backed approval state + audit log.

Survives process restarts so a pending approval is never lost or double-applied.
A fresh connection is opened per call (low volume, thread-safe across Bolt's
handler threads and the observer thread).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from config import settings
from .models import ApprovalStatus, Diagnosis, Incident


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS pending_approvals (
                id              TEXT PRIMARY KEY,
                workload_kind   TEXT NOT NULL,
                workload_name   TEXT NOT NULL,
                namespace       TEXT NOT NULL,
                container_name  TEXT NOT NULL,
                reason          TEXT NOT NULL,
                diagnosis       TEXT NOT NULL,
                confidence      REAL NOT NULL,
                patch_json      TEXT NOT NULL,
                status          TEXT NOT NULL,
                slack_channel   TEXT,
                slack_ts        TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_id TEXT,
                workload    TEXT,
                event       TEXT NOT NULL,
                detail      TEXT,
                actor       TEXT,
                created_at  TEXT NOT NULL
            );
            """
        )


def create_pending(incident: Incident, diagnosis: Diagnosis) -> str:
    approval_id = uuid.uuid4().hex
    now = _now()
    with _conn() as c:
        c.execute(
            """
            INSERT INTO pending_approvals (
                id, workload_kind, workload_name, namespace, container_name,
                reason, diagnosis, confidence, patch_json, status,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                approval_id,
                incident.workload_kind,
                incident.workload_name,
                incident.namespace,
                incident.container_name,
                incident.reason.value,
                diagnosis.diagnosis,
                diagnosis.confidence,
                json.dumps(diagnosis.patch),
                ApprovalStatus.PENDING.value,
                now,
                now,
            ),
        )
    audit(approval_id, f"{incident.workload_kind}/{incident.workload_name}",
          "proposed", diagnosis.diagnosis, actor="kubeheal")
    return approval_id


def get_pending(approval_id: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM pending_approvals WHERE id = ?", (approval_id,)).fetchone()
    return dict(row) if row else None


def set_slack_ref(approval_id: str, channel: str, ts: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE pending_approvals SET slack_channel = ?, slack_ts = ?, updated_at = ? WHERE id = ?",
            (channel, ts, _now(), approval_id),
        )


def update_status(approval_id: str, status: ApprovalStatus) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE pending_approvals SET status = ?, updated_at = ? WHERE id = ?",
            (status.value, _now(), approval_id),
        )


def audit(approval_id: str | None, workload: str | None, event: str,
          detail: str | None = None, actor: str | None = None) -> None:
    with _conn() as c:
        c.execute(
            "INSERT INTO audit_log (approval_id, workload, event, detail, actor, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (approval_id, workload, event, detail, actor, _now()),
        )

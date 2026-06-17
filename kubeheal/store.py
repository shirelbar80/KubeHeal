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

# Statuses for an incident still awaiting a human decision — an approval not yet
# clicked, or a "needs a human" notice not yet acknowledged. These are the rows
# we dedupe against and send reminders for.
_OPEN_STATUSES = (ApprovalStatus.PENDING.value, ApprovalStatus.NEEDS_HUMAN.value)


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
                updated_at      TEXT NOT NULL,
                last_reminded_at TEXT
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

            CREATE TABLE IF NOT EXISTS workload_cooldown (
                workload_key    TEXT PRIMARY KEY,
                last_alerted_at TEXT NOT NULL
            );
            """
        )
        # Migration for DBs created before last_reminded_at existed.
        cols = [r["name"] for r in c.execute("PRAGMA table_info(pending_approvals)").fetchall()]
        if "last_reminded_at" not in cols:
            c.execute("ALTER TABLE pending_approvals ADD COLUMN last_reminded_at TEXT")


def should_alert(workload_key: str, cooldown_seconds: int) -> bool:
    """Return True and record the time if this workload is outside its cooldown
    window; False (suppress) if it alerted too recently. Survives restarts."""
    now = datetime.now(timezone.utc)
    with _conn() as c:
        row = c.execute(
            "SELECT last_alerted_at FROM workload_cooldown WHERE workload_key = ?",
            (workload_key,),
        ).fetchone()
        if row:
            last = datetime.fromisoformat(row["last_alerted_at"])
            if (now - last).total_seconds() < cooldown_seconds:
                return False
        c.execute(
            "INSERT INTO workload_cooldown (workload_key, last_alerted_at) VALUES (?, ?) "
            "ON CONFLICT(workload_key) DO UPDATE SET last_alerted_at = excluded.last_alerted_at",
            (workload_key, now.isoformat()),
        )
    return True


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


def create_needs_human(incident: Incident, detail: str) -> str:
    """Record an incident KubeHeal can't fix in scope, so it can be deduped,
    reminded, and acknowledged just like an approval (it has no patch)."""
    incident_id = uuid.uuid4().hex
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
                incident_id,
                incident.workload_kind,
                incident.workload_name,
                incident.namespace,
                incident.container_name,
                incident.reason.value,
                detail[:500],          # store the reason in the diagnosis column
                0.0,                   # no confidence — there's no patch
                "{}",                  # no patch
                ApprovalStatus.NEEDS_HUMAN.value,
                now,
                now,
            ),
        )
    audit(incident_id, f"{incident.workload_kind}/{incident.workload_name}",
          "needs_human", detail, actor="kubeheal")
    return incident_id


def get_pending(approval_id: str) -> dict[str, Any] | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM pending_approvals WHERE id = ?", (approval_id,)).fetchone()
    return dict(row) if row else None


def has_open_incident(namespace: str, workload_kind: str, workload_name: str) -> bool:
    """True if an unresolved incident (pending approval OR un-acknowledged
    "needs a human") exists for this workload — used to suppress duplicates."""
    placeholders = ",".join("?" for _ in _OPEN_STATUSES)
    with _conn() as c:
        row = c.execute(
            "SELECT 1 FROM pending_approvals "
            f"WHERE namespace = ? AND workload_kind = ? AND workload_name = ? "
            f"AND status IN ({placeholders}) LIMIT 1",
            (namespace, workload_kind, workload_name, *_OPEN_STATUSES),
        ).fetchone()
    return row is not None


def list_due_reminders(reminder_seconds: int) -> list[dict[str, Any]]:
    """Open incidents (pending approval or un-acknowledged needs-a-human) whose
    last reminder (or creation) is older than the interval and that have a Slack
    message to reply to."""
    now = datetime.now(timezone.utc)
    due: list[dict[str, Any]] = []
    placeholders = ",".join("?" for _ in _OPEN_STATUSES)
    with _conn() as c:
        rows = c.execute(
            f"SELECT * FROM pending_approvals WHERE status IN ({placeholders})",
            _OPEN_STATUSES,
        ).fetchall()
    for row in rows:
        if not row["slack_ts"]:
            continue
        ref = row["last_reminded_at"] or row["created_at"]
        if (now - datetime.fromisoformat(ref)).total_seconds() >= reminder_seconds:
            due.append(dict(row))
    return due


def mark_reminded(approval_id: str) -> None:
    with _conn() as c:
        c.execute(
            "UPDATE pending_approvals SET last_reminded_at = ? WHERE id = ?",
            (_now(), approval_id),
        )


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

"""Tests for the SQLite store: open-approval dedupe + reminder scheduling."""

import sqlite3

import pytest

from config import settings
from kubeheal import store
from kubeheal.models import ApprovalStatus, Diagnosis, FailureReason, Incident


@pytest.fixture
def db(tmp_path, monkeypatch):
    # _conn() reads settings.db_path on every call, so patching the attribute
    # is enough — no module reload needed.
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "kh.sqlite"))
    store.init_db()


def _make():
    inc = Incident(
        pod_name="p", namespace="kubeheal-demo", workload_name="oom-demo",
        reason=FailureReason.OOM_KILLED, container_name="hog",
    )
    diag = Diagnosis(
        diagnosis="x", root_cause="y", confidence=0.9,
        patch={"spec": {"template": {"spec": {"containers": [{"name": "hog"}]}}}},
        patch_explanation="z",
    )
    return inc, diag


def test_has_open_incident_approval_lifecycle(db):
    inc, diag = _make()
    aid = store.create_pending(inc, diag)
    assert store.has_open_incident("kubeheal-demo", "Deployment", "oom-demo") is True
    # once actioned, it's no longer "open"
    store.update_status(aid, ApprovalStatus.APPLIED)
    assert store.has_open_incident("kubeheal-demo", "Deployment", "oom-demo") is False


def test_needs_human_is_open_until_acknowledged(db):
    inc, _ = _make()
    nid = store.create_needs_human(inc, "image change required — out of scope")
    # a needs-a-human notice counts as an open incident (so it dedupes + reminds)
    assert store.has_open_incident("kubeheal-demo", "Deployment", "oom-demo") is True
    store.update_status(nid, ApprovalStatus.ACKNOWLEDGED)
    assert store.has_open_incident("kubeheal-demo", "Deployment", "oom-demo") is False


def test_needs_human_gets_reminders(db):
    inc, _ = _make()
    nid = store.create_needs_human(inc, "out of scope")
    store.set_slack_ref(nid, "C123", "1700000000.000200")
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE pending_approvals SET created_at = ? WHERE id = ?",
                 ("2000-01-01T00:00:00+00:00", nid))
    conn.commit()
    conn.close()
    assert any(r["id"] == nid for r in store.list_due_reminders(900))
    # acknowledging removes it from the reminder set
    store.update_status(nid, ApprovalStatus.ACKNOWLEDGED)
    assert all(r["id"] != nid for r in store.list_due_reminders(900))


def test_reminder_not_due_without_slack_ref(db):
    inc, diag = _make()
    store.create_pending(inc, diag)  # no slack_ts set -> never reminded
    assert store.list_due_reminders(900) == []


def test_reminder_becomes_due_then_cleared(db):
    inc, diag = _make()
    aid = store.create_pending(inc, diag)
    store.set_slack_ref(aid, "C123", "1700000000.000100")

    # Not due immediately.
    assert all(r["id"] != aid for r in store.list_due_reminders(900))

    # Backdate creation so the reminder is overdue.
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE pending_approvals SET created_at = ? WHERE id = ?",
                 ("2000-01-01T00:00:00+00:00", aid))
    conn.commit()
    conn.close()

    assert any(r["id"] == aid for r in store.list_due_reminders(900))

    # After reminding, it's no longer due.
    store.mark_reminded(aid)
    assert all(r["id"] != aid for r in store.list_due_reminders(900))

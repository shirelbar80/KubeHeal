"""ChatOps interface — Slack Bolt app in Socket Mode.

Posts a Block Kit alert (diagnosis, confidence, proposed patch) with Approve /
Reject buttons and handles the actions. Socket Mode opens an outbound WebSocket
using the App-Level token, so no public URL / ngrok is needed.

Text-override (natural-language patch rewrite) is intentionally NOT built here —
it's a deferred Phase 3 stretch (see PLAN.md).
"""

from __future__ import annotations

import json

from slack_bolt import App

from config import settings

from .models import ApprovalStatus, Diagnosis, Incident
from .remediator import apply_patch
from . import store


def _alert_blocks(approval_id: str, incident: Incident, diagnosis: Diagnosis) -> list[dict]:
    patch_str = json.dumps(diagnosis.patch, indent=2)
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🚨 {incident.reason.value}: {incident.workload_name}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Workload:*\n{incident.workload_kind}/{incident.workload_name}"},
                {"type": "mrkdwn", "text": f"*Namespace:*\n{incident.namespace}"},
                {"type": "mrkdwn", "text": f"*Container:*\n{incident.container_name}"},
                {"type": "mrkdwn", "text": f"*Confidence:*\n{diagnosis.confidence:.0%}"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Diagnosis:* {diagnosis.diagnosis}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Root cause:* {diagnosis.root_cause}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Proposed fix:* {diagnosis.patch_explanation}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Patch:*\n```{patch_str}```"}},
        {
            "type": "actions",
            "block_id": "kubeheal_actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "✅ Approve Patch"},
                    "action_id": "approve_patch",
                    "value": approval_id,
                },
                {
                    "type": "button",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "❌ Reject"},
                    "action_id": "reject_patch",
                    "value": approval_id,
                },
            ],
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"id: `{approval_id}`"}]},
    ]


def _resolved_blocks(header: str, lines: list[str]) -> list[dict]:
    blocks: list[dict] = [{"type": "header", "text": {"type": "plain_text", "text": header}}]
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    return blocks


def build_app() -> App:
    app = App(token=settings.slack_bot_token)

    @app.action("reject_patch")
    def on_reject(ack, body, client):  # noqa: ANN001
        ack()
        approval_id = body["actions"][0]["value"]
        user = body.get("user", {}).get("username") or body.get("user", {}).get("id", "unknown")
        row = store.get_pending(approval_id)
        if not row or row["status"] != ApprovalStatus.PENDING.value:
            return
        store.update_status(approval_id, ApprovalStatus.REJECTED)
        store.audit(approval_id, f"{row['workload_kind']}/{row['workload_name']}", "rejected", actor=user)
        client.chat_update(
            channel=body["channel"]["id"],
            ts=body["message"]["ts"],
            text="Patch rejected",
            blocks=_resolved_blocks(
                f"❌ Rejected: {row['workload_name']}",
                [f"Rejected by <@{body['user']['id']}>. No changes were applied."],
            ),
        )

    @app.action("approve_patch")
    def on_approve(ack, body, client):  # noqa: ANN001
        ack()
        approval_id = body["actions"][0]["value"]
        user = body.get("user", {}).get("username") or body.get("user", {}).get("id", "unknown")
        row = store.get_pending(approval_id)
        if not row or row["status"] != ApprovalStatus.PENDING.value:
            return

        store.update_status(approval_id, ApprovalStatus.APPROVED)
        workload = f"{row['workload_kind']}/{row['workload_name']}"
        store.audit(approval_id, workload, "approved", actor=user)

        channel, ts = body["channel"]["id"], body["message"]["ts"]
        client.chat_update(
            channel=channel, ts=ts, text="Applying patch…",
            blocks=_resolved_blocks(
                f"⏳ Applying: {row['workload_name']}",
                [f"Approved by <@{body['user']['id']}>. Dry-running and applying…"],
            ),
        )

        patch = json.loads(row["patch_json"])
        result = apply_patch(row["workload_name"], row["namespace"], row["container_name"], patch)

        if result.ok:
            status, header, icon = ApprovalStatus.APPLIED, f"✅ Healed: {row['workload_name']}", "✅"
        elif result.rolled_back:
            status, header, icon = ApprovalStatus.ROLLED_BACK, f"↩️ Rolled back: {row['workload_name']}", "↩️"
        else:
            status, header, icon = ApprovalStatus.FAILED, f"⚠️ Failed: {row['workload_name']}", "⚠️"

        store.update_status(approval_id, status)
        store.audit(approval_id, workload, status.value, result.message, actor=user)
        client.chat_update(
            channel=channel, ts=ts, text=result.message,
            blocks=_resolved_blocks(header, [f"{icon} {result.message}", f"Approved by <@{body['user']['id']}>."]),
        )

    return app


def post_incident(app: App, incident: Incident, diagnosis: Diagnosis) -> str:
    """Create the pending approval and post the Block Kit alert. Returns its id."""
    approval_id = store.create_pending(incident, diagnosis)
    resp = app.client.chat_postMessage(
        channel=settings.slack_channel,
        text=f"{incident.reason.value} in {incident.workload_name} — approval needed",
        blocks=_alert_blocks(approval_id, incident, diagnosis),
    )
    store.set_slack_ref(approval_id, resp["channel"], resp["ts"])
    return approval_id

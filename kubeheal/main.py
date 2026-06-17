"""Entrypoint — wires the Observer, the Brain, and the Slack app together.

Run with:  py -m kubeheal.main

The Observer runs in a background thread. Each detected incident is diagnosed by
the Brain and posted to Slack in its own worker thread (so a slow LLM call never
blocks the watch). The Slack Socket Mode handler runs on the main thread.
"""

from __future__ import annotations

import threading
import time

from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import settings

from . import observer, probefix, rollback, store
from .brain import BrainError, diagnose
from .logging_setup import configure, get_logger, kv
from .models import AUTO_REMEDIABLE_REASONS, FailureReason, Incident
from .slack_app import (
    build_app,
    post_incident,
    post_reminder,
    post_rollback_proposal,
    post_unremediable,
)

log = get_logger("kubeheal.main")

# Human-facing hints for failures KubeHeal can detect but not fix in scope.
_OUT_OF_SCOPE_HINTS = {
    FailureReason.IMAGE_PULL_BACKOFF:
        "Image pull is failing — check the image name/tag, the registry, and imagePullSecrets.",
    FailureReason.CONFIG_ERROR:
        "A referenced ConfigMap or Secret is missing or invalid — create it or fix the reference.",
}


def _unfixable_detail(incident: Incident) -> str:
    """A reason-specific hint plus the most telling Pod event line, if any."""
    hint = _OUT_OF_SCOPE_HINTS.get(
        incident.reason, "This failure is outside KubeHeal's fixable scope (resources + probes)."
    )
    cause = next(
        (ln.strip() for ln in incident.events.splitlines()
         if any(k in ln for k in ("Failed", "not found", "ErrImagePull", "Error"))),
        "",
    )
    return f"{hint}\n{cause}".strip() if cause else hint


def _require_config() -> None:
    missing = [
        name
        for name, value in (
            ("SLACK_BOT_TOKEN", settings.slack_bot_token),
            ("SLACK_APP_TOKEN", settings.slack_app_token),
            ("SLACK_CHANNEL", settings.slack_channel),
        )
        if not value
    ]
    if missing:
        raise SystemExit(f"Missing required config: {', '.join(missing)}. Set them in .env.")


def main() -> None:
    configure()
    _require_config()
    store.init_db()
    app = build_app()

    log.info("starting %s", kv(namespace=settings.namespace, model=settings.ollama_model,
                               channel=settings.slack_channel))

    def process(incident: Incident) -> None:
        workload = f"{incident.workload_kind}/{incident.workload_name}"

        # First: is this a failing recent deploy? If so, the safest fix is to
        # revert to the previous revision — deterministic, no LLM. This also
        # rescues deploy-induced image/config failures.
        info = rollback.assess(incident.workload_name, incident.namespace,
                               settings.rollback_window_seconds)
        if info is not None:
            log.info("bad-rollout incident %s", kv(workload=workload,
                     from_rev=info.from_revision, to_rev=info.to_revision))
            try:
                approval_id = post_rollback_proposal(app, incident, info)
                log.info("posted rollback proposal %s", kv(id=approval_id, workload=workload))
            except Exception as post_exc:  # noqa: BLE001
                log.error("failed to post rollback proposal %s", kv(workload=workload, error=post_exc))
            return

        # A slow-starting container killed by its liveness probe has a reliable,
        # deterministic fix (add a startupProbe) — don't depend on the LLM.
        slowfix = probefix.assess(incident)
        if slowfix is not None:
            approval_id = post_incident(app, incident, slowfix)
            log.info("posted startup-probe fix %s", kv(id=approval_id, workload=workload))
            return

        # Reasons outside the allow-list (bad image, missing config) can't be
        # fixed by a resources/probes patch — skip the LLM and notify a human.
        if incident.reason not in AUTO_REMEDIABLE_REASONS:
            log.info("out-of-scope incident %s", kv(workload=workload, reason=incident.reason.value))
            try:
                incident_id = post_unremediable(app, incident, _unfixable_detail(incident))
                log.info("posted needs-a-human %s", kv(id=incident_id, workload=workload))
            except Exception as post_exc:  # noqa: BLE001
                log.error("failed to post unremediable notice %s", kv(workload=workload, error=post_exc))
            return

        try:
            diagnosis = diagnose(incident)
        except BrainError as exc:
            log.error("brain failed %s", kv(workload=workload, error=exc))
            try:
                incident_id = post_unremediable(app, incident, str(exc))
                log.info("posted needs-a-human %s", kv(id=incident_id, workload=workload))
            except Exception as post_exc:  # noqa: BLE001 - never let Slack errors crash the worker
                log.error("failed to post unremediable notice %s", kv(workload=workload, error=post_exc))
            return
        approval_id = post_incident(app, incident, diagnosis)
        log.info("posted approval %s", kv(id=approval_id, workload=workload,
                                          confidence=f"{diagnosis.confidence:.2f}"))

    def on_incident(incident: Incident) -> None:
        threading.Thread(target=process, args=(incident,), daemon=True).start()

    def reminder_loop() -> None:
        """Periodically re-ping Slack about pending approvals no one has acted on."""
        while True:
            try:
                for row in store.list_due_reminders(settings.reminder_seconds):
                    try:
                        post_reminder(app, row)
                        store.mark_reminded(row["id"])
                        log.info("reminder %s", kv(id=row["id"], workload=row["workload_name"]))
                    except Exception as exc:  # noqa: BLE001 - one bad reminder shouldn't stop the loop
                        log.error("reminder failed %s", kv(id=row["id"], error=exc))
            except Exception as exc:  # noqa: BLE001
                log.error("reminder loop error %s", kv(error=exc))
            time.sleep(60)

    obs = threading.Thread(target=observer.run, args=(on_incident,), daemon=True)
    obs.start()
    threading.Thread(target=reminder_loop, daemon=True).start()

    log.info("starting Slack Socket Mode handler")
    SocketModeHandler(app, settings.slack_app_token).start()


if __name__ == "__main__":
    main()

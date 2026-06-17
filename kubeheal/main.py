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

from . import observer, store
from .brain import BrainError, diagnose
from .logging_setup import configure, get_logger, kv
from .models import Incident
from .slack_app import build_app, post_incident, post_reminder, post_unremediable

log = get_logger("kubeheal.main")


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

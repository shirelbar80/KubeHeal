"""Entrypoint — wires the Observer, the Brain, and the Slack app together.

Run with:  py -m kubeheal.main

The Observer runs in a background thread. Each detected incident is diagnosed by
the Brain and posted to Slack in its own worker thread (so a slow LLM call never
blocks the watch). The Slack Socket Mode handler runs on the main thread.
"""

from __future__ import annotations

import threading

from slack_bolt.adapter.socket_mode import SocketModeHandler

from config import settings

from . import observer, store
from .brain import BrainError, diagnose
from .models import Incident
from .slack_app import build_app, post_incident


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
    _require_config()
    store.init_db()
    app = build_app()

    print(f"[kubeheal] namespace={settings.namespace} model={settings.ollama_model} "
          f"channel={settings.slack_channel}")

    def process(incident: Incident) -> None:
        try:
            diagnosis = diagnose(incident)
        except BrainError as exc:
            print(f"[kubeheal] brain failed for {incident.workload_name}: {exc}")
            store.audit(None, f"{incident.workload_kind}/{incident.workload_name}",
                        "diagnosis_failed", str(exc), actor="kubeheal")
            return
        approval_id = post_incident(app, incident, diagnosis)
        print(f"[kubeheal] posted approval {approval_id} for {incident.workload_name}")

    def on_incident(incident: Incident) -> None:
        threading.Thread(target=process, args=(incident,), daemon=True).start()

    obs = threading.Thread(target=observer.run, args=(on_incident,), daemon=True)
    obs.start()

    print("[kubeheal] starting Slack Socket Mode handler…")
    SocketModeHandler(app, settings.slack_app_token).start()


if __name__ == "__main__":
    main()

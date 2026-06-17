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
from .logging_setup import configure, get_logger, kv
from .models import AUTO_REMEDIABLE_REASONS, FailureReason, Incident
from .slack_app import build_app, post_incident, post_unremediable

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

        # Reasons outside the allow-list (bad image, missing config) can't be
        # fixed by a resources/probes patch — skip the LLM and notify a human.
        if incident.reason not in AUTO_REMEDIABLE_REASONS:
            log.info("out-of-scope incident %s", kv(workload=workload, reason=incident.reason.value))
            store.audit(None, workload, "out_of_scope", incident.reason.value, actor="kubeheal")
            try:
                post_unremediable(app, incident, _unfixable_detail(incident))
            except Exception as post_exc:  # noqa: BLE001
                log.error("failed to post unremediable notice %s", kv(workload=workload, error=post_exc))
            return

        try:
            diagnosis = diagnose(incident)
        except BrainError as exc:
            log.error("brain failed %s", kv(workload=workload, error=exc))
            store.audit(None, workload, "diagnosis_failed", str(exc), actor="kubeheal")
            try:
                post_unremediable(app, incident, str(exc))
            except Exception as post_exc:  # noqa: BLE001 - never let Slack errors crash the worker
                log.error("failed to post unremediable notice %s", kv(workload=workload, error=post_exc))
            return
        approval_id = post_incident(app, incident, diagnosis)
        log.info("posted approval %s", kv(id=approval_id, workload=workload,
                                          confidence=f"{diagnosis.confidence:.2f}"))

    def on_incident(incident: Incident) -> None:
        threading.Thread(target=process, args=(incident,), daemon=True).start()

    obs = threading.Thread(target=observer.run, args=(on_incident,), daemon=True)
    obs.start()

    log.info("starting Slack Socket Mode handler")
    SocketModeHandler(app, settings.slack_app_token).start()


if __name__ == "__main__":
    main()

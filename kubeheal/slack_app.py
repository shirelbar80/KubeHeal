"""ChatOps interface — Slack Bolt app in Socket Mode (Phase 3).

Posts a Block Kit alert (diagnosis, confidence, rendered patch) with Approve /
Reject buttons, and handles the button actions. No public URL / ngrok needed —
Socket Mode opens an outbound WebSocket using the App-Level token.

Text-override (natural-language patch rewrite) is DEFERRED to a later Phase 3
stretch and is intentionally not built in the MVP.

TODO(Phase 3): Block Kit builder, approve/reject handlers, Socket Mode startup.
"""

from __future__ import annotations


def build_app():  # pragma: no cover - stub
    raise NotImplementedError("Slack app is implemented in Phase 3.")

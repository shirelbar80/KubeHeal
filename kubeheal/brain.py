"""The Brain — local LLM diagnosis via Ollama (Phase 2).

Sends the system prompt + incident (logs, reason, current spec) to Ollama with
``format="json"``, then validates the response against ``Diagnosis``. On invalid
JSON: one corrective retry, then fail gracefully. LLM output is never trusted
blindly — it always passes through ``safety`` before reaching the cluster.

TODO(Phase 2): implement Ollama call, JSON-schema validation, retry.
"""

from __future__ import annotations

from .models import Diagnosis, Incident


def diagnose(incident: Incident) -> Diagnosis:  # pragma: no cover - stub
    raise NotImplementedError("Brain is implemented in Phase 2.")

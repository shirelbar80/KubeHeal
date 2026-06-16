"""The Brain — local LLM diagnosis via Ollama.

Sends the system prompt + incident to a local Ollama model, constraining the
output to the ``Diagnosis`` JSON schema. The result is validated against the
schema AND the safety allow-list; on failure we feed the error back and retry a
couple of times before giving up. The model output is never trusted blindly.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from ollama import Client
from pydantic import ValidationError

from config import settings

from .logging_setup import get_logger, kv
from .models import Diagnosis, Incident
from .safety import UnsafePatchError, validate_patch

log = get_logger("kubeheal.brain")

# camelCase patch key -> snake_case client attr in current_spec.
_PROBE_KEYS = {
    "livenessProbe": "liveness_probe",
    "readinessProbe": "readiness_probe",
    "startupProbe": "startup_probe",
}

_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "sre_system_prompt.txt"
_MAX_ATTEMPTS = 3


class BrainError(RuntimeError):
    """Raised when the LLM cannot produce a valid, safe diagnosis."""


@lru_cache(maxsize=1)
def _system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _client() -> Client:
    return Client(host=settings.ollama_host)


def _incident_prompt(incident: Incident) -> str:
    events = incident.events.strip() or "(no events)"
    # Separate read-only context (ports) from the patchable spec so the model
    # doesn't echo non-allowed fields like `ports` into its patch.
    spec = dict(incident.current_spec)
    ports = spec.pop("ports", None)
    ports_line = (
        f"Declared container ports (READ-ONLY context, never put in the patch): "
        f"{json.dumps(ports)}\n"
        if ports
        else ""
    )
    return (
        f"Failure reason: {incident.reason.value}\n"
        f"Deployment: {incident.workload_name}\n"
        f"Container: {incident.container_name}\n"
        f"{ports_line}"
        f"Current patchable spec (resources + probes):\n{json.dumps(spec, indent=2)}\n\n"
        f"Recent Pod events:\n{events}\n\n"
        f"Recent logs:\n{incident.logs.strip()}\n"
    )


def _strip_invented_probes(patch: dict, incident: Incident) -> None:
    """Remove probe fields the model added that weren't already on the container.

    Small models tend to bolt on plausible-looking probes (e.g. an HTTP probe on
    a port nothing serves), which then fail and block recovery. Policy: modifying
    an EXISTING probe is allowed; inventing a NEW one is not. Mutates in place.
    """
    existing = {camel for camel, snake in _PROBE_KEYS.items()
                if incident.current_spec.get(snake) is not None}
    containers = patch.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
    for container in containers:
        for camel in _PROBE_KEYS:
            if camel in container and camel not in existing:
                del container[camel]
                log.info("stripped invented probe %s", kv(workload=incident.workload_name, probe=camel))


def diagnose(incident: Incident) -> Diagnosis:
    """Return a validated, safety-checked ``Diagnosis`` for the incident."""
    messages = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": _incident_prompt(incident)},
    ]
    schema = Diagnosis.model_json_schema()
    last_error = ""

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        response = _client().chat(
            model=settings.ollama_model,
            messages=messages,
            format=schema,
            options={"temperature": 0},
        )
        content = response.message.content or ""

        try:
            diagnosis = Diagnosis.model_validate_json(content)
            _strip_invented_probes(diagnosis.patch, incident)
            validate_patch(diagnosis.patch)
            return diagnosis
        except (ValidationError, json.JSONDecodeError, UnsafePatchError) as exc:
            last_error = str(exc)
            # Feed the model its own bad output plus the specific error, and retry.
            messages.append({"role": "assistant", "content": content})
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "That response was invalid: "
                        f"{last_error}\n"
                        "Return ONLY a corrected JSON object that satisfies all the "
                        "rules. Remember: patch may only modify resources/probes on "
                        "the named container."
                    ),
                }
            )

    raise BrainError(f"LLM failed to produce a valid diagnosis after {_MAX_ATTEMPTS} attempts: {last_error}")

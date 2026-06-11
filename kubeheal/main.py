"""Entrypoint — wires the Observer and the Slack app together.

Run with:  py -m kubeheal.main

TODO: start observer loop + Slack Socket Mode app concurrently; graceful
shutdown. Filled in incrementally across Phases 1-3.
"""

from __future__ import annotations

from config import settings


def main() -> None:  # pragma: no cover - stub
    print(f"KubeHeal starting (namespace={settings.namespace}, model={settings.ollama_model})")
    raise NotImplementedError("Wiring is completed across Phases 1-3.")


if __name__ == "__main__":
    main()

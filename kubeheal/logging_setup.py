"""Structured logging setup.

Emits one line per event as ``ts level logger | key=value …`` so logs are both
human-readable and easy to grep/parse. Call ``configure()`` once at startup.
"""

from __future__ import annotations

import logging
import sys


def configure(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def kv(**fields: object) -> str:
    """Render key=value pairs for a log message."""
    return " ".join(f"{k}={v}" for k, v in fields.items())

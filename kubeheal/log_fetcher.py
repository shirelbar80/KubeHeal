"""Pull recent logs from a failing pod (Phase 1).

Fetches the last N lines from the current container and, importantly, also the
*previous* container instance (``previous=True``) — that's where a crashed
container's logs live after a restart.

TODO(Phase 1): implement read_namespaced_pod_log (current + previous).
"""

from __future__ import annotations


def fetch_logs(pod_name: str, namespace: str, tail_lines: int = 50) -> str:  # pragma: no cover - stub
    raise NotImplementedError("Log fetcher is implemented in Phase 1.")

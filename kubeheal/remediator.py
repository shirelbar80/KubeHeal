"""The Remediator — applies an approved patch safely (Phase 3).

Flow: server-side dry-run -> real apply -> verify recovery -> report.
Keeps the previous spec to enable rollback if post-patch verification fails.

TODO(Phase 3): dry-run patch, real patch, recovery verification, rollback.
"""

from __future__ import annotations

from typing import Any


def apply_patch(
    workload_name: str,
    namespace: str,
    patch: dict[str, Any],
) -> None:  # pragma: no cover - stub
    raise NotImplementedError("Remediator is implemented in Phase 3.")

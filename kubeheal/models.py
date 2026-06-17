"""Shared data contracts passed between KubeHeal components.

These models are the single source of truth for the shape of an incident, the
LLM's diagnosis, and the proposed patch. The Brain validates raw LLM output
against ``Diagnosis`` before anything reaches the cluster.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class FailureReason(str, Enum):
    CRASH_LOOP_BACKOFF = "CrashLoopBackOff"
    OOM_KILLED = "OOMKilled"
    IMAGE_PULL_BACKOFF = "ImagePullBackOff"
    CONFIG_ERROR = "CreateContainerConfigError"
    ERROR = "Error"
    OTHER = "Other"


# Failures KubeHeal might fix within its allow-list (resources + probes). Other
# reasons (image pull, missing config) are inherently out of scope — they need a
# change KubeHeal isn't allowed to make, so they route straight to "needs a human"
# instead of wasting LLM calls that can only fail.
AUTO_REMEDIABLE_REASONS: set[FailureReason] = {
    FailureReason.CRASH_LOOP_BACKOFF,
    FailureReason.OOM_KILLED,
}


class Incident(BaseModel):
    """A detected failure, assembled by the Observer before calling the Brain."""

    pod_name: str
    namespace: str
    workload_kind: str = "Deployment"   # owner resolved from the pod
    workload_name: str
    reason: FailureReason
    container_name: str
    logs: str = ""                       # last N lines (incl. previous container)
    events: str = ""                     # recent Pod events (e.g. probe failures)
    current_spec: dict[str, Any] = Field(default_factory=dict)  # relevant container spec


class Diagnosis(BaseModel):
    """Validated LLM output. The ``patch`` is a strategic-merge patch for the
    workload, restricted by the safety allow-list before use."""

    diagnosis: str
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    patch: dict[str, Any]
    patch_explanation: str


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"

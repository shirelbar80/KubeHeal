"""Tests for Diagnosis parsing/validation (no LLM call required)."""

import json

import pytest
from pydantic import ValidationError

from kubeheal.models import Diagnosis
from kubeheal.safety import UnsafePatchError, validate_patch

_GOOD_PATCH = {
    "spec": {"template": {"spec": {"containers": [
        {"name": "hog", "resources": {"limits": {"memory": "128Mi"}}}
    ]}}}
}


def test_parses_valid_diagnosis():
    raw = json.dumps(
        {
            "diagnosis": "OOMKilled",
            "root_cause": "memory limit too low",
            "confidence": 0.9,
            "patch": _GOOD_PATCH,
            "patch_explanation": "raise memory limit",
        }
    )
    d = Diagnosis.model_validate_json(raw)
    assert d.confidence == 0.9
    validate_patch(d.patch)  # safe patch passes


def test_rejects_out_of_range_confidence():
    with pytest.raises(ValidationError):
        Diagnosis(
            diagnosis="x", root_cause="y", confidence=1.5,
            patch=_GOOD_PATCH, patch_explanation="z",
        )


def test_rejects_missing_field():
    with pytest.raises(ValidationError):
        Diagnosis.model_validate_json(json.dumps({"diagnosis": "x"}))


def test_strip_invented_probes():
    from kubeheal.brain import _strip_invented_probes
    from kubeheal.models import FailureReason, Incident

    # Container had NO probes -> an invented livenessProbe must be removed,
    # while the resources change is kept.
    oom = Incident(
        pod_name="p", namespace="n", workload_name="oom-demo",
        reason=FailureReason.OOM_KILLED, container_name="hog",
        current_spec={"name": "hog", "resources": {"limits": {"memory": "10Mi"}}},
    )
    patch = {"spec": {"template": {"spec": {"containers": [
        {"name": "hog", "resources": {"limits": {"memory": "40Mi"}},
         "livenessProbe": {"httpGet": {"path": "/healthz", "port": 8080}}}
    ]}}}}
    _strip_invented_probes(patch, oom)
    c = patch["spec"]["template"]["spec"]["containers"][0]
    assert "livenessProbe" not in c
    assert c["resources"]["limits"]["memory"] == "40Mi"


def test_keeps_existing_probe_modification():
    from kubeheal.brain import _strip_invented_probes
    from kubeheal.models import FailureReason, Incident

    # Container HAD a livenessProbe -> modifying it is allowed (kept).
    bad = Incident(
        pod_name="p", namespace="n", workload_name="badprobe-demo",
        reason=FailureReason.CRASH_LOOP_BACKOFF, container_name="web",
        current_spec={"name": "web", "liveness_probe": {"httpGet": {"port": 8080}}},
    )
    patch = {"spec": {"template": {"spec": {"containers": [
        {"name": "web", "livenessProbe": {"httpGet": {"path": "/", "port": 80}}}
    ]}}}}
    _strip_invented_probes(patch, bad)
    c = patch["spec"]["template"]["spec"]["containers"][0]
    assert c["livenessProbe"]["httpGet"]["port"] == 80


def test_unsafe_patch_from_model_is_caught():
    d = Diagnosis(
        diagnosis="x", root_cause="y", confidence=0.5,
        patch={"spec": {"template": {"spec": {"containers": [
            {"name": "app", "image": "evil:latest"}
        ]}}}},
        patch_explanation="swap image",
    )
    with pytest.raises(UnsafePatchError):
        validate_patch(d.patch)

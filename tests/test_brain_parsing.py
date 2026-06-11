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

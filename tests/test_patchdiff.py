"""Tests for the human-readable patch diff renderer."""

from kubeheal.patchdiff import render_diff


def _patch(container: dict) -> dict:
    return {"spec": {"template": {"spec": {"containers": [container]}}}}


def test_memory_bump_diff():
    current = {"name": "hog", "resources": {"limits": {"memory": "10Mi"}, "requests": {"memory": "10Mi"}}}
    patch = _patch({"name": "hog", "resources": {"limits": {"memory": "40Mi"}, "requests": {"memory": "20Mi"}}})
    lines = render_diff(current, patch)
    assert "resources.limits.memory: 10Mi → 40Mi" in lines
    assert "resources.requests.memory: 10Mi → 20Mi" in lines


def test_probe_port_change_diff():
    current = {"name": "web", "liveness_probe": {"httpGet": {"path": "/", "port": 8080}}}
    patch = _patch({"name": "web", "livenessProbe": {"httpGet": {"path": "/", "port": 80}}})
    lines = render_diff(current, patch)
    assert "livenessProbe.httpGet.port: 8080 → 80" in lines
    # unchanged leaf (path) is not reported
    assert all("httpGet.path" not in ln for ln in lines)


def test_added_field_shows_none_as_old():
    current = {"name": "web", "liveness_probe": {"httpGet": {"port": 8080}}}
    patch = _patch({"name": "web", "startupProbe": {"httpGet": {"port": 8080}, "failureThreshold": 30}})
    lines = render_diff(current, patch)
    assert "startupProbe.httpGet.port: (none) → 8080" in lines
    assert "startupProbe.failureThreshold: (none) → 30" in lines


def test_no_change_returns_empty():
    current = {"name": "hog", "resources": {"limits": {"memory": "40Mi"}}}
    patch = _patch({"name": "hog", "resources": {"limits": {"memory": "40Mi"}}})
    assert render_diff(current, patch) == []


def test_malformed_patch_is_safe():
    assert render_diff({}, {"spec": {}}) == []

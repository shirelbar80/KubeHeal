"""Tests for the patch safety allow-list."""

import pytest

from kubeheal.safety import UnsafePatchError, validate_patch


def _patch(container: dict) -> dict:
    return {"spec": {"template": {"spec": {"containers": [container]}}}}


def test_allows_resource_bump():
    validate_patch(_patch({"name": "hog", "resources": {"limits": {"memory": "128Mi"}}}))


def test_allows_probes():
    validate_patch(
        _patch(
            {
                "name": "app",
                "livenessProbe": {"httpGet": {"path": "/", "port": 8080}},
                "readinessProbe": {"tcpSocket": {"port": 8080}},
            }
        )
    )


def test_rejects_image_change():
    with pytest.raises(UnsafePatchError):
        validate_patch(_patch({"name": "app", "image": "evil/registry:latest"}))


def test_rejects_securitycontext():
    with pytest.raises(UnsafePatchError):
        validate_patch(_patch({"name": "app", "securityContext": {"privileged": True}}))


def test_rejects_missing_container_name():
    with pytest.raises(UnsafePatchError):
        validate_patch(_patch({"resources": {"limits": {"memory": "128Mi"}}}))


def test_rejects_extra_toplevel_key():
    with pytest.raises(UnsafePatchError):
        validate_patch({"spec": {"template": {"spec": {"containers": []}}}, "metadata": {"x": 1}})


def test_rejects_replicas():
    with pytest.raises(UnsafePatchError):
        validate_patch({"spec": {"replicas": 5}})


def test_rejects_non_cpu_memory_resource_key():
    with pytest.raises(UnsafePatchError):
        validate_patch(
            _patch({"name": "app", "resources": {"limits": {"nvidia.com/gpu": "1"}}})
        )


def test_rejects_empty_containers():
    with pytest.raises(UnsafePatchError):
        validate_patch({"spec": {"template": {"spec": {"containers": []}}}})


def test_allows_valid_integer_probe_port():
    validate_patch(
        _patch({"name": "app", "livenessProbe": {"httpGet": {"path": "/", "port": 8080},
                                                 "periodSeconds": 10}})
    )


def test_rejects_object_probe_port():
    # This is exactly the malformed shape the 2B model emitted (caught pre-dry-run now).
    with pytest.raises(UnsafePatchError):
        validate_patch(
            _patch({"name": "app", "livenessProbe": {"httpGet": {"port": {"number": 8080}}}})
        )


def test_rejects_probe_without_handler():
    with pytest.raises(UnsafePatchError):
        validate_patch(_patch({"name": "app", "readinessProbe": {"periodSeconds": 5}}))


def test_rejects_unknown_probe_field():
    with pytest.raises(UnsafePatchError):
        validate_patch(
            _patch({"name": "app", "livenessProbe": {"httpGet": {"port": 80}, "bogus": 1}})
        )

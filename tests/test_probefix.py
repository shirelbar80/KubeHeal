"""Tests for the deterministic slow-start (startupProbe) fix."""

from kubeheal.models import FailureReason, Incident
from kubeheal.probefix import assess

_PROBE_EVENTS = ("Warning Unhealthy: Liveness probe failed: connection refused\n"
                 "Normal Killing: Container web failed liveness probe, will be restarted")


def _incident(current_spec, events=_PROBE_EVENTS):
    return Incident(
        pod_name="p", namespace="n", workload_name="slowstart-demo",
        reason=FailureReason.CRASH_LOOP_BACKOFF, container_name="web",
        events=events, current_spec=current_spec,
    )


def _slowstart_spec():
    return {
        "name": "web",
        "ports": [{"containerPort": 8080}],
        "liveness_probe": {"httpGet": {"path": "/", "port": 8080}, "initialDelaySeconds": 0},
    }


def test_proposes_startup_probe_when_port_matches():
    d = assess(_incident(_slowstart_spec()))
    assert d is not None
    sp = d.patch["spec"]["template"]["spec"]["containers"][0]["startupProbe"]
    assert sp["httpGet"]["port"] == 8080
    assert sp["failureThreshold"] >= 10  # generous startup budget


def test_none_when_port_mismatch():
    # Wrong port -> not a clean slow-start; the LLM handles it.
    spec = _slowstart_spec()
    spec["liveness_probe"]["httpGet"]["port"] = 9090
    assert assess(_incident(spec)) is None


def test_none_when_not_probe_related():
    assert assess(_incident(_slowstart_spec(), events="Warning BackOff: Back-off restarting")) is None


def test_none_when_startup_probe_already_present():
    spec = _slowstart_spec()
    spec["startup_probe"] = {"httpGet": {"path": "/", "port": 8080}}
    assert assess(_incident(spec)) is None


def test_none_when_no_probe():
    assert assess(_incident({"name": "web", "ports": [{"containerPort": 8080}]})) is None

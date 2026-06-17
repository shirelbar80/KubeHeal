"""Tests for the Observer's failure classifier (_classify)."""

from types import SimpleNamespace

from kubeheal.models import FailureReason
from kubeheal.observer import _classify


def _status(waiting=None, terminated=None, last_terminated=None, name="app"):
    """Build a minimal stand-in for a V1ContainerStatus."""
    state = SimpleNamespace(
        waiting=SimpleNamespace(reason=waiting) if waiting else None,
        terminated=SimpleNamespace(reason=terminated) if terminated else None,
    )
    last_state = SimpleNamespace(
        waiting=None,
        terminated=SimpleNamespace(reason=last_terminated) if last_terminated else None,
    )
    return SimpleNamespace(state=state, last_state=last_state, name=name)


def test_imagepullbackoff():
    assert _classify(_status(waiting="ImagePullBackOff")) == (FailureReason.IMAGE_PULL_BACKOFF, "app")


def test_errimagepull_maps_to_image_pull():
    assert _classify(_status(waiting="ErrImagePull")) == (FailureReason.IMAGE_PULL_BACKOFF, "app")


def test_invalid_image_name_maps_to_image_pull():
    assert _classify(_status(waiting="InvalidImageName")) == (FailureReason.IMAGE_PULL_BACKOFF, "app")


def test_create_container_config_error():
    assert _classify(_status(waiting="CreateContainerConfigError")) == (FailureReason.CONFIG_ERROR, "app")


def test_crashloop_still_classified():
    assert _classify(_status(waiting="CrashLoopBackOff")) == (FailureReason.CRASH_LOOP_BACKOFF, "app")


def test_crashloop_with_oom_last_state_is_oom():
    assert _classify(
        _status(waiting="CrashLoopBackOff", last_terminated="OOMKilled")
    ) == (FailureReason.OOM_KILLED, "app")


def test_oomkilled_terminated():
    assert _classify(_status(terminated="OOMKilled")) == (FailureReason.OOM_KILLED, "app")


def test_healthy_returns_none():
    assert _classify(_status()) is None


def test_unknown_waiting_reason_returns_none():
    # e.g. ContainerCreating during normal startup — not a failure.
    assert _classify(_status(waiting="ContainerCreating")) is None

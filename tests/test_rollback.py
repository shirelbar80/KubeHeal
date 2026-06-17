"""Tests for rollout-undo detection logic (no cluster required)."""

from types import SimpleNamespace

from kubeheal.rollback import _pick_target, _substantive_change


def _container(name="web", image="nginx:1.0", command=None, args=None, env=None):
    return SimpleNamespace(name=name, image=image, command=command, args=args, env=env)


def _rs(revision, name=None, containers=None):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=name or f"rs-{revision}",
            annotations={"deployment.kubernetes.io/revision": str(revision)},
        ),
        spec=SimpleNamespace(
            template=SimpleNamespace(spec=SimpleNamespace(containers=containers or [_container()])),
        ),
    )


def test_pick_target_chooses_current_and_previous():
    rs1, rs2, rs3 = _rs(1), _rs(2), _rs(3)
    current, previous = _pick_target([rs2, rs1, rs3])  # unordered
    assert current is rs3
    assert previous is rs2


def test_pick_target_none_with_single_revision():
    assert _pick_target([_rs(1)]) is None


def test_pick_target_ignores_rs_without_revision():
    no_rev = SimpleNamespace(metadata=SimpleNamespace(name="x", annotations={}),
                             spec=SimpleNamespace(template=None))
    assert _pick_target([_rs(1), no_rev]) is None  # only one valid revision


def test_substantive_change_true_on_image_diff():
    cur = _rs(2, containers=[_container(image="nginx:bad")])
    prev = _rs(1, containers=[_container(image="nginx:1.27-alpine")])
    assert _substantive_change(cur, prev) is True


def test_substantive_change_false_on_identical_template():
    # e.g. a `rollout restart` — new revision, same substantive spec.
    cur = _rs(2, containers=[_container(image="nginx:1.0")])
    prev = _rs(1, containers=[_container(image="nginx:1.0")])
    assert _substantive_change(cur, prev) is False


def test_substantive_change_true_on_command_diff():
    cur = _rs(2, containers=[_container(command=["a"])])
    prev = _rs(1, containers=[_container(command=["b"])])
    assert _substantive_change(cur, prev) is True

"""Core ownership is no-throw; extensions report failure after root commit."""
from pathlib import Path

import pytest

from pal.execution.result_snapshots import ResultSnapshotStore
from pal.memory.history_root import HistoryRoot, SUMMARY_TURN_ID
from pal.memory.turn_ir import L1TurnStore
from tests.test_history_root_authority import _Candidate
from tests.test_result_snapshots import result_turn


def test_extension_failure_is_reported_after_coherent_commit():
    from pal.memory.contracts import L2Entry
    from tests.test_v3_n1_root_lifecycle import service_with_work

    memory = service_with_work()
    root = memory.history_root
    right = root.right_turns()
    root.begin_compact("compact", reason="manual")
    entry = L2Entry(entry_id="summary", kind="summary", scope="session",
                    title="Summary", summary="new left", rendered="new left")
    seen = []

    def extension():
        assert root.run_record("compact").phase.value == "committed"
        assert root.cut.turn_count == 1
        assert root.right_turns() == right
        seen.append("committed")
        raise RuntimeError("extension notification failed")

    outcome = memory.compact_left("compact", entry, candidate_id="candidate", after_commit=extension)
    assert seen == ["committed"]
    assert outcome.status == "committed"
    assert "Post-commit cleanup failed" in outcome.detail
    assert "extension notification failed" in outcome.detail
    assert root.right_turns() == right
    assert root.commit("compact").replayed


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_core_ownership_does_not_swallow_process_control(failure, tmp_path, monkeypatch):
    snapshots = ResultSnapshotStore(tmp_path)
    history = L1TurnStore()
    snapshots.bind_history(history)
    ref = snapshots.capture("copy", call_id="call", lifetime="turn")

    def broken(ref):
        raise failure()

    monkeypatch.setattr(snapshots, "_validate_path", broken)
    with pytest.raises(failure):
        snapshots._listeners[id(history)]((), (result_turn(ref),))


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit])
def test_core_diagnostic_does_not_swallow_process_control(failure, tmp_path, monkeypatch):
    snapshots = ResultSnapshotStore(tmp_path)
    history = L1TurnStore()
    snapshots.bind_history(history)
    ref = snapshots.capture("copy", call_id="call", lifetime="turn")

    def broken_validation(ref):
        raise RuntimeError("retention failure")

    def broken_log(*args, **kwargs):
        raise failure()

    monkeypatch.setattr(snapshots, "_validate_path", broken_validation)
    monkeypatch.setattr("pal.execution.result_snapshots.LOG.exception", broken_log)
    with pytest.raises(failure):
        snapshots._listeners[id(history)]((), (result_turn(ref),))


@pytest.mark.parametrize("diagnostic_fails", [False, True])
def test_snapshot_retention_failure_preserves_owners_and_inhibits_later_reap(tmp_path, monkeypatch, caplog, diagnostic_fails):
    snapshots = ResultSnapshotStore(tmp_path)
    old_ref = snapshots.capture("old retained", call_id="old", lifetime="turn")
    new_ref = snapshots.capture("new retained", call_id="new", lifetime="turn")
    root = HistoryRoot(L1TurnStore([result_turn(old_ref, "left-1"), result_turn(old_ref, "left-2")]))
    snapshots.bind_history(root.store)
    snapshots.finish_delivery(lifetime="turn", call_id="old")
    root.promote()
    root.begin_right_turn("right", user_text="keep right")
    right = root.right_turns()
    old_owners = {key: set(value) for key, value in snapshots._owners.items()}
    root.begin_compact("compact", reason="manual")
    root.mark_ready("compact", _Candidate("summary"), candidate_id="candidate")
    validate = snapshots._validate_path
    validations = 0

    def fail_after_prevalidation(ref):
        nonlocal validations
        validations += 1
        if validations == 2:
            raise RuntimeError("retention observer failed")
        validate(ref)

    monkeypatch.setattr(snapshots, "_validate_path", fail_after_prevalidation)
    if diagnostic_fails:
        def broken_log(*args, **kwargs):
            raise RuntimeError("logging handler failed")
        monkeypatch.setattr("pal.execution.result_snapshots.LOG.exception", broken_log)
    notified = []
    root.store.add_change_listener(lambda old, new: notified.append(new))
    outcome = root.commit("compact", build_summary_turn=lambda _: result_turn(new_ref, SUMMARY_TURN_ID))
    assert len(notified) == 1
    assert outcome.status == "committed"
    assert root.cut.turn_count == 1
    assert root.right_turns() == right
    assert snapshots._owners == old_owners
    if not diagnostic_fails:
        assert "retention observer failed" in caplog.text
        assert "reclamation disabled" in caplog.text
    monkeypatch.setattr(snapshots, "_validate_path", validate)
    snapshots.finish_delivery(lifetime="turn", call_id="new")
    snapshots.release("unrelated")
    # A later successful incremental notification cannot repair a missed update.
    root.begin_right_turn("later", user_text="another input")
    snapshots.reap()
    # Restore's orphan sweep must respect the same conservative boundary.
    untracked = snapshots.root / ("f" * 32 + ".txt")
    untracked.write_text("ownership uncertain")
    snapshots.finish_restore()
    assert untracked.exists()
    assert Path(old_ref.path).read_text() == "old retained"
    assert Path(new_ref.path).read_text() == "new retained"


@pytest.mark.parametrize("diagnostic_fails", [False, True])
def test_post_transfer_cleanup_failure_cannot_abort_commit(tmp_path, monkeypatch, caplog, diagnostic_fails):
    from tests.test_history_root_authority import _Base

    root = _Base().root_with_promoted_left()
    snapshots = ResultSnapshotStore(tmp_path)
    snapshots.bind_history(root.store)
    root.begin_compact("compact", reason="manual")
    root.mark_ready("compact", _Candidate("summary"), candidate_id="candidate")

    def broken_reap():
        raise RuntimeError("unexpected cleanup failure")

    def broken_log(*args, **kwargs):
        raise RuntimeError("logging handler failed")

    monkeypatch.setattr(snapshots, "reap", broken_reap)
    if diagnostic_fails:
        monkeypatch.setattr("pal.execution.result_snapshots.LOG.exception", broken_log)
    assert root.commit("compact").status == "committed"
    assert root.cut.turn_count == 1
    assert root.active_run is None
    if not diagnostic_fails:
        assert "unexpected cleanup failure" in caplog.text


def test_reap_filesystem_failure_keeps_reference_and_can_retry(tmp_path, monkeypatch, caplog):
    snapshots = ResultSnapshotStore(tmp_path)
    ref = snapshots.capture("retire later", call_id="call", lifetime="turn")
    unlink = Path.unlink

    def failed_unlink(path, *args, **kwargs):
        raise OSError("temporary cleanup failure")

    monkeypatch.setattr(Path, "unlink", failed_unlink)
    snapshots.finish_delivery(lifetime="turn", call_id="call")
    assert ref in snapshots.references()
    assert Path(ref.path).exists()
    assert "temporary cleanup failure" in caplog.text
    monkeypatch.setattr(Path, "unlink", unlink)
    snapshots.reap()
    assert ref not in snapshots.references()
    assert not Path(ref.path).exists()

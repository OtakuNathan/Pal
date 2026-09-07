from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pal.bunshin.compact import BUNSHIN_COMPACTION_SYSTEM_PROMPT, BunshinCompactionPolicy
from pal.bunshin.runner import BunshinRunner
from pal.shared import BunshinInvocationPack


async def noop(*args, **kwargs):
    return None


def runner_at(root: Path, *, bound: bool = False) -> BunshinRunner:
    return BunshinRunner(
        runtime_root=root,
        pack=BunshinInvocationPack(
            invocation_id="progress-test",
            workspace={"architect_path": str(root / "architect.yaml")},
            metadata={"bunshin_v2": {"role": "architect"}} if bound else {},
        ),
        bunshin_id="progress-test", run_id="attempt-test",
        write_event=noop, read_decision=noop,
    )


def test_documented_nonempty_checkpoint_passes_real_validator():
    example = BUNSHIN_COMPACTION_SYSTEM_PROMPT.split(
        "Valid non-empty example (shape only; never copy example facts into the checkpoint): ", 1
    )[1].split("\n", 1)[0]
    checkpoint = BunshinCompactionPolicy().validate_checkpoint(example, None)
    assert all(json.loads(example)["continuity"].values())
    assert checkpoint.summary


def test_ledger_content_not_version_or_call_count_drives_progress(tmp_path):
    runner = runner_at(tmp_path, bound=True)
    ledger = {"version": 1, "items": [
        {"kind": "task", "summary": "resolve finding: f1", "status": "pending"}
    ]}
    with patch("pal.bunshin.v2.work_items.read_work_items", return_value=ledger):
        before = runner._completion_gate_progress_marker()
        ledger["version"] = 2
        ledger["items"][0]["item_id"] = "new-bookkeeping-id"
        runner._observed_tool_call_count = 200
        assert runner._completion_gate_progress_marker() == before
        ledger["items"][0]["status"] = "completed"
        completed = runner._completion_gate_progress_marker()
        assert completed != before
        ledger["items"].append({"kind": "finding", "status": "completed",
                                "summary": "Missing handoff", "finding": {"priority": "p1"}})
        finding = runner._completion_gate_progress_marker()
        assert finding != completed
        ledger["items"][-1]["finding"]["priority"] = "p2"
        assert runner._completion_gate_progress_marker() != finding


def test_same_path_changed_content_counts_but_touch_does_not(tmp_path):
    runner = runner_at(tmp_path)
    output = tmp_path / "architect.yaml"
    before = runner._completion_gate_progress_marker()
    output.write_text("first")
    created = runner._completion_gate_progress_marker()
    assert created != before
    output.touch()
    assert runner._completion_gate_progress_marker() == created
    output.write_text("other")
    assert runner._completion_gate_progress_marker() != created
    output.unlink()
    assert runner._completion_gate_progress_marker() == before


def test_registered_staged_artifact_content_is_observed(tmp_path):
    runner = runner_at(tmp_path)
    path = tmp_path / "stage.json"
    path.write_text("first")
    runner.produced_artifacts.append({"stage_path": str(path), "path": str(tmp_path / "final.json")})
    before = runner._completion_gate_progress_marker()
    path.write_text("other")
    assert runner._completion_gate_progress_marker() != before


def test_unavailable_ledger_does_not_masquerade_as_stagnation(tmp_path):
    runner = runner_at(tmp_path, bound=True)
    with patch("pal.bunshin.v2.work_items.read_work_items", side_effect=RuntimeError("gateway unavailable")):
        with pytest.raises(RuntimeError, match="gateway unavailable"):
            runner._completion_gate_progress_marker()


def test_read_only_activity_cannot_keep_missing_submission_alive(tmp_path):
    class ReadingRunner(BunshinRunner):
        calls = 0

        async def _run_agent_loop(self, bundle, *, forced_retry_note=""):
            self.calls += 1
            self._observed_tool_call_count += 5
            return "done"

    base = runner_at(tmp_path)
    base.pack.workspace["output_policy"] = {"primary_artifact": "expected.json"}
    runner = ReadingRunner(runtime_root=tmp_path, pack=base.pack,
                           bunshin_id="test", run_id="test", write_event=noop, read_decision=noop)
    asyncio.run(runner._run_v2_invocation(None))
    assert runner.calls == 2
    assert runner.blocked_kind == "completion_gate_stalled"


def test_checklist_progress_allows_another_completion_attempt(tmp_path):
    ledger = {"version": 1, "items": [
        {"kind": "task", "summary": "resolve finding: f1", "status": "pending"}
    ]}

    class RepairingRunner(BunshinRunner):
        calls = 0

        async def _run_agent_loop(self, bundle, *, forced_retry_note=""):
            self.calls += 1
            if self.calls == 2:
                ledger["items"][0]["status"] = "completed"
            return "done"

    base = runner_at(tmp_path, bound=True)
    base.pack.workspace["output_policy"] = {"primary_artifact": "expected.json"}
    runner = RepairingRunner(runtime_root=tmp_path, pack=base.pack,
                            bunshin_id="test", run_id="test", write_event=noop, read_decision=noop)
    with patch("pal.bunshin.v2.work_items.read_work_items", return_value=ledger):
        asyncio.run(runner._run_v2_invocation(None))
    assert runner.calls == 3
    assert runner.blocked_kind == "completion_gate_stalled"

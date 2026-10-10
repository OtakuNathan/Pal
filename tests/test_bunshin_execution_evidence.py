"""Failure identity uses execution bytes, independently of result presentation."""
import hashlib
import shlex
import sys

import pytest

from pal.bunshin.review_receipts import _review_tool_evidence_ref
from pal.bunshin.semantic_orchestration.verification_settlement import _repair_failure_fingerprint
from pal.core import PalCore
from pal.execution import register_with_core
from pal.execution.contracts import ToolCallBudget
from pal.shared.tool_protocol import new_tool_call


@pytest.fixture
def runtime(tmp_path):
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities("execution")
    runtime = core.context.execution_runtime
    runtime.configure_runtime_root(tmp_path / "runtime")
    runtime.begin_tool_result_turn(turn_id="verification", scope_key="failure-evidence")
    yield runtime
    runtime.shutdown()


def execution_details(result):
    return result.structured.get("details", result.structured)


def execute_failure(runtime, *, marker="Expected 2, got 1", padding=0, budget=1800):
    input_path = runtime.result_snapshots.root.parent.parent / "failure-input.txt"
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text(marker)
    source = ("import sys; from pathlib import Path; sys.stdout.write('context\\n'); "
              f"sys.stderr.write('H' * {padding} + Path({str(input_path)!r}).read_text() + 'T' * {padding}); sys.exit(1)")
    call = new_tool_call(name="run_shell", args={"cmd": shlex.join([sys.executable, "-c", source])})
    result = runtime.execute_tool(call, turn_id="verification",
        budget=ToolCallBudget(max_output_chars=budget, preview_chars=300))
    assert execution_details(result)["returncode"] == 1
    assert not result.ok
    return result, _review_tool_evidence_ref("op_exec_shell", call, result)


def fingerprint(receipt):
    return _repair_failure_fingerprint("repair", [{"finding_kind": "module_defect", "summary": "Wrong output"}], [receipt])


def test_same_failure_under_snapshot_path_with_spaces(runtime, tmp_path):
    runtime.configure_runtime_root(tmp_path / "runtime with spaces")
    first, a = execute_failure(runtime, padding=100000)
    second, b = execute_failure(runtime, padding=100000)
    assert first.snapshot_refs and second.snapshot_refs
    assert first.snapshot_refs[0].path != second.snapshot_refs[0].path
    assert "runtime with spaces" in first.snapshot_refs[0].path
    assert fingerprint(a) == fingerprint(b)


def test_program_snapshot_like_diagnostics_remain_significant(runtime):
    _, first = execute_failure(runtime, marker="Output snapshot: /actual/diagnostic-one")
    _, second = execute_failure(runtime, marker="Output snapshot: /actual/diagnostic-two")
    assert fingerprint(first) != fingerprint(second)


def test_change_in_hidden_middle_of_stderr_changes_fingerprint(runtime):
    first, a = execute_failure(runtime, marker="MIDDLE_ERROR_A", padding=100000)
    second, b = execute_failure(runtime, marker="MIDDLE_ERROR_B", padding=100000)
    assert "MIDDLE_ERROR_A" not in a["output_text"]
    assert "MIDDLE_ERROR_B" not in b["output_text"]
    assert execution_details(first)["stderr_truncated"] and execution_details(second)["stderr_truncated"]
    assert fingerprint(a) != fingerprint(b)


def test_complete_stream_digests_do_not_depend_on_delivery_budget(runtime):
    full, a = execute_failure(runtime, padding=2000, budget=50000)
    preview, b = execute_failure(runtime, padding=2000, budget=1800)
    assert not execution_details(full)["stderr_truncated"]
    assert execution_details(preview)["stderr_truncated"]
    expected = hashlib.sha256(("H" * 2000 + "Expected 2, got 1" + "T" * 2000).encode()).hexdigest()
    assert execution_details(full)["stderr_sha256"] == expected
    assert execution_details(preview)["stderr_sha256"] == expected
    assert execution_details(full)["stdout_sha256"] == hashlib.sha256(b"context\n").hexdigest()
    assert fingerprint(a) == fingerprint(b)

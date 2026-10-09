"""Workflow adapters retain evidence attached to successful legacy results."""
import asyncio
from types import SimpleNamespace

from pal.core import PalCore  # Import-order bootstrap.
from pal.bunshin.scoped_execution import _workflow_capability
from pal.bunshin.candidate_builder import CANDIDATE_BUILDER_TOOL_SPECS
from pal.execution.runtime import ExecutionRuntime
from pal.shared import MountedSubtreeHandle, ToolExecutionResult
from pal.shared.tool_protocol import ToolContextMessageIR, new_tool_call


def test_workflow_success_keeps_empty_payload_and_evidence(tmp_path):
    runtime = ExecutionRuntime(runtime_root=tmp_path)
    ref = runtime.result_snapshots.capture("original evidence", call_id="source", lifetime="review")
    contexts = (ToolContextMessageIR(content="reference evidence", semantic_kind="reference"),)
    calls = []

    def handler(call, _meta):
        calls.append(call.call_id)
        return ToolExecutionResult(
            name=call.name, ok=True, text="completed", llm_text="completed", structured={},
            snapshot_refs=(ref,), context_messages=contexts, context_delivery={"reference": "kept"},
        )

    descriptor, action = _workflow_capability(
        name="op_bunshin_candidate_submit",
        spec=CANDIDATE_BUILDER_TOOL_SPECS["op_bunshin_candidate_submit"], handler=handler,
    )
    subtree = MountedSubtreeHandle(module_id="workflow_scoped")
    subtree.descriptors.append(descriptor)
    subtree.bound_actions.append(action)
    subtree.bound_action_keys.append((action.canonical_path, action.target_id))
    subtree.search_record_ids.append(descriptor.name)
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=subtree))
    try:
        result = asyncio.run(runtime.execute_tool_async(new_tool_call(name="submit_candidate", args={})))
        assert result.ok, result.llm_text
        assert result.snapshot_refs == (ref,)
        assert result.context_messages == contexts
        assert result.context_delivery == {"reference": "kept"}
        assert result.structured == {"payload": {}}
        assert len(calls) == 1
    finally:
        runtime.shutdown()

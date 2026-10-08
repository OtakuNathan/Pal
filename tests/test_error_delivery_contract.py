"""Model-visible failures survive remote adapters, prompt modes and delivery faults."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pal.bunshin.prompt_adapter import BunshinPromptFragmentProvider
from pal.core.prompt import MinimalOperatingRulesPromptFragmentProvider
from pal.execution.contracts import CapabilityResult, ToolCallBudget
from pal.execution.tool_facade import EffectOutcome, EffectReceipt, RetryDirective, ToolRejectedError
from pal.foundation.sidecar import (
    SidecarEndpoint, cleanup_sidecar_endpoint, dispatch_sidecar_request,
    handle_sidecar_client, start_sidecar_server,
)
from pal.mcp.compiler import McpCompiler
from pal.mcp.ipc import McpManagerClient, McpManagerRpcError
from pal.mcp.model import McpDiscoverySnapshot, McpPromptSpec, McpToolSpec
from pal.mcp.normalize import normalize_protocol_error
from pal.shared import PromptAssemblyContext, RuntimeStatus
from pal.shared.tool_protocol import ToolAffordance, new_tool_call
from tests.test_prompt_infrastructure import _compiler
from tests.test_tool_failure_affordances import runtime, invoke, metadata
from tests.test_tool_result_fidelity import mount


@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("turn_kind", ["chat", "failure"])
def test_every_tool_using_prompt_explains_all_effects_and_retries(worker, turn_kind):
    provider = (BunshinPromptFragmentProvider(scaffold_factory=lambda: {}, role_context_factory=lambda: "")
                if worker else MinimalOperatingRulesPromptFragmentProvider())
    prompt = _compiler(provider).build_prompt_ir(PromptAssemblyContext(
        turn_kind=turn_kind, core_mode="bunshin" if worker else "pal",
        metadata={"memory_pack": None, "failure_rules": "Repair only the reported blocker."}))
    system = "\n".join(block.content for block in prompt.system_blocks)
    for outcome in EffectOutcome:
        assert f"effect={outcome.value}" in system
    for directive in RetryDirective:
        assert f"retry={directive.value}" in system
    assert "error_code" in system and "affordances" in system


@pytest.mark.parametrize("kind", ["tool", "prompt"])
def test_mcp_protocol_failure_keeps_chain_and_transport_details(runtime, kind):
    def fail(*args):
        try:
            raise PermissionError("original remote cause; token=hidden-token")
        except PermissionError as cause:
            raise McpManagerRpcError("transport wrapper", payload={"failed_stage": "response decode"}) from cause
    invoker = SimpleNamespace(call_tool=fail, render_prompt=fail)
    snapshot = McpDiscoverySnapshot(server_id="fidelity", transport="stdio",
        tools=(McpToolSpec(name="probe", input_schema={"type": "object"}),) if kind == "tool" else (),
        prompts=(McpPromptSpec(name="probe"),) if kind == "prompt" else ()).with_hash()
    projection = McpCompiler().compile(module_id="mcp_fidelity", snapshots=(snapshot,), invoker=invoker)
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=projection.mounted_subtree))
    result = invoke(runtime, "call_mcp_fidelity_probe" if kind == "tool" else "render_mcp_fidelity_probe", {})
    assert not result.ok
    assert "original remote cause" in result.llm_text and "response decode" in result.llm_text
    assert "hidden-token" not in result.llm_text
    assert metadata(result)["error_code"] == "mcp_protocol_error"
    assert metadata(result)["effect"] == "unknown"


def test_remote_exception_chain_survives_actual_sidecar_transport(runtime, tmp_path):
    async def run():
        endpoint = SidecarEndpoint(tmp_path, "mcp")
        async def call_method(method, params):
            try:
                raise OSError("remote root cause; token=hidden-remote")
            except OSError as cause:
                raise RuntimeError("remote wrapper") from cause
        async def dispatch(request):
            return await dispatch_sidecar_request(request, call_method)
        async def handle(reader, writer):
            await handle_sidecar_client(reader, writer, dispatch)
        server, _ = await start_sidecar_server(endpoint, handle)
        try:
            with pytest.raises(McpManagerRpcError) as caught:
                await McpManagerClient(tmp_path).call_tool("demo", "probe", {})
            return normalize_protocol_error(caught.value, server_id="demo", name="probe", kind="tool")
        finally:
            server.close()
            await server.wait_closed()
            await cleanup_sidecar_endpoint(endpoint)
    raw = asyncio.run(run())
    mount(runtime, "remote_failure", lambda _: raw)
    result = invoke(runtime, "remote_failure", {})
    assert "remote root cause" in result.llm_text
    assert "remote wrapper" in result.llm_text
    assert "hidden-remote" not in result.llm_text
    assert metadata(result)["error_code"] == "mcp_protocol_error"


@pytest.mark.parametrize("resolver", ["failure", "final"])
def test_guidance_fault_preserves_original_recovery_and_reports_its_own_cause(runtime, monkeypatch, resolver):
    hint = "Repair only the failed item; never repeat the completed upload."
    mount(runtime, "guidance_fault", lambda _: CapabilityResult(
        status=RuntimeStatus.ERROR, text="original failure", llm_text="original failure",
        structured={"error_code": "original_error"}, recovery_hint=hint,
        effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED)))
    def fail(*args, **kwargs):
        raise RuntimeError("guidance resolver crashed")
    if resolver == "failure":
        monkeypatch.setattr("pal.execution.runtime.resolve_failure_guidance", fail)
    else:
        monkeypatch.setattr(runtime, "_resolve_result_guidance", fail)
    result = invoke(runtime, "guidance_fault", {})
    assert not result.ok and metadata(result)["error_code"] == "original_error"
    assert "original failure" in result.llm_text
    assert hint in result.llm_text
    assert "guidance resolver crashed" in result.llm_text
    assert metadata(result)["effect"] == "applied"


def test_storage_fault_cannot_discard_original_failure_cause(runtime, monkeypatch):
    original = "FIRST_CAUSE\n" + "x" * 4000 + "\nMIDDLE_CAUSE\n" + "y" * 4000 + "\nLAST_CAUSE"
    mount(runtime, "unsaved_failure", lambda _: CapabilityResult(
        status=RuntimeStatus.ERROR, text=original, llm_text=original,
        structured={"error_code": "original_error"},
        effect_receipt=EffectReceipt(outcome=EffectOutcome.UNKNOWN)))
    def fail(*args, **kwargs):
        raise OSError("diagnostic storage failed")
    monkeypatch.setattr(runtime.result_snapshots, "capture", fail)
    result = runtime.execute_tool(new_tool_call(name="unsaved_failure", args={}), turn_id="review",
        budget=ToolCallBudget(max_output_chars=1200, preview_chars=300))
    assert not result.ok and metadata(result)["error_code"] == "original_error"
    assert original in result.llm_text
    assert "diagnostic storage failed" in result.llm_text
    assert not result.snapshot_refs


@pytest.mark.parametrize("relay", [False, True])
def test_rejection_keeps_the_reason_its_precondition_failed(runtime, relay):
    def fail(_):
        try:
            raise OSError("precondition lookup failed")
        except OSError as cause:
            raise ToolRejectedError("precondition unavailable", error_code="precondition_unavailable") from cause
    mount(runtime, "rejection_chain", fail, direct=not relay)
    if relay:
        from pal.execution.contracts import CapabilityCall
        mount(runtime, "rejection_relay", lambda _: runtime.execute(CapabilityCall(name="rejection_chain")))
    result = invoke(runtime, "rejection_relay" if relay else "rejection_chain", {})
    assert "precondition lookup failed" in result.llm_text
    assert "precondition unavailable" in result.llm_text
    assert metadata(result)["kind"] == "rejected"
    assert metadata(result)["effect"] == "not_started"


def test_individual_recovery_suggestion_fault_reaches_model(runtime, monkeypatch):
    mount(runtime, "suggestion_fault", lambda _: CapabilityResult(
        status=RuntimeStatus.ERROR, text="original failure", llm_text="original failure",
        structured={"error_code": "original_error"},
        affordances=(ToolAffordance(tool="read_file", arguments={"file_path": "unused"}, reason="inspect state"),)))
    def fail(*args, **kwargs):
        raise RuntimeError("suggestion validation crashed")
    monkeypatch.setattr("pal.execution.runtime.action_key", fail)
    result = invoke(runtime, "suggestion_fault", {})
    assert not result.ok and metadata(result)["error_code"] == "original_error"
    assert "suggestion validation crashed" in result.llm_text
    assert not result.invocation_result.affordances

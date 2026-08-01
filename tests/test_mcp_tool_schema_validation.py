from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

from types import SimpleNamespace

import pytest

from pal.core import PalCore
from pal.execution.tool_facade import CompleteResult, FailedResult, RejectedResult
from pal.mcp.compiler import McpCompiler
from pal.mcp.model import McpDiscoverySnapshot, McpToolSpec


class _Invoker:
    def __init__(self, result: dict[str, object] | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.result = result or {"content": [{"type": "text", "text": "ok"}]}

    def call_tool(self, _server_id: str, _tool_name: str, arguments: dict[str, object]) -> dict[str, object]:
        self.calls.append(dict(arguments))
        return dict(self.result)

    def render_prompt(self, _server_id: str, _prompt_name: str, _arguments: dict[str, object]) -> dict[str, object]:
        return {"messages": []}


MCP_SCHEMA = {
    "$defs": {
        "config": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 1, "maximum": 3},
                "tags": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["a", "b"]},
                    "minItems": 1,
                    "maxItems": 2,
                },
            },
            "required": ["count", "tags"],
            "additionalProperties": False,
        }
    },
    "type": "object",
    "properties": {
        "kind": {"const": "job"},
        "mode": {"type": "string", "enum": ["fast", "safe"]},
        "config": {"$ref": "#/$defs/config"},
        "choice": {
            "oneOf": [
                {"type": "string", "minLength": 2},
                {"type": "integer", "minimum": 10},
            ]
        },
    },
    "required": ["kind", "mode", "config", "choice"],
    "additionalProperties": False,
}


def _runtime_with_mcp():
    invoker = _Invoker()
    snapshot = McpDiscoverySnapshot(
        server_id="schema",
        transport="stdio",
        tools=(McpToolSpec(name="validate", input_schema=MCP_SCHEMA),),
    ).with_hash()
    projection = McpCompiler().compile(module_id="mcp_schema", snapshots=(snapshot,), invoker=invoker)
    runtime = PalCore().context.execution_runtime
    runtime.mount_subtree(
        SimpleNamespace(mounted_subtree=projection.mounted_subtree)
    )
    return runtime, invoker


def test_mcp_schema_keeps_draft_2020_12_contract_and_fixed_output_shape() -> None:
    runtime, invoker = _runtime_with_mcp()
    record = runtime.registry_generation.indirect_aliases["mcp_schema_validate"]
    assert record.input_model is None
    assert "$defs" in record.input_schema

    value = {
        "kind": "job",
        "mode": "fast",
        "config": {"count": 1, "tags": ["a"]},
        "choice": "ok",
    }
    result = runtime.invoke_indirect_tool(new_tool_call(name=record.alias, args=value))
    assert isinstance(result, CompleteResult)
    assert invoker.calls == [value]
    assert result.output == {
        "content": [{"type": "text", "text": "ok"}],
        "structured_content": None,
        "is_error": False,
    }


@pytest.mark.parametrize(
    "value",
    [
        {"mode": "fast", "config": {"count": 1, "tags": ["a"]}, "choice": "ok"},
        {"kind": "task", "mode": "fast", "config": {"count": 1, "tags": ["a"]}, "choice": "ok"},
        {"kind": "job", "mode": "other", "config": {"count": 1, "tags": ["a"]}, "choice": "ok"},
        {"kind": "job", "mode": "fast", "config": {"count": 0, "tags": ["a"]}, "choice": "ok"},
        {"kind": "job", "mode": "fast", "config": {"count": 1, "tags": []}, "choice": "ok"},
        {"kind": "job", "mode": "fast", "config": {"count": 1, "tags": ["c"]}, "choice": "ok"},
        {"kind": "job", "mode": "fast", "config": {"count": 1, "tags": ["a"], "extra": True}, "choice": "ok"},
        {"kind": "job", "mode": "fast", "config": {"count": 1, "tags": ["a"]}, "choice": 9},
        {"kind": "job", "mode": "fast", "config": {"count": 1, "tags": ["a"]}, "choice": "ok", "extra": True},
    ],
)
def test_mcp_schema_rejects_type_required_extra_enum_const_composition_and_bounds(value) -> None:
    runtime, invoker = _runtime_with_mcp()
    result = runtime.invoke_indirect_tool(new_tool_call(name="mcp_schema_validate", args=value))
    assert isinstance(result, RejectedResult)
    assert result.error_code == "invalid_arguments"
    assert not invoker.calls


def test_mcp_defaults_indirect_but_can_declare_direct() -> None:
    invoker = _Invoker()
    snapshot = McpDiscoverySnapshot(
        server_id="mode",
        transport="stdio",
        tools=(
            McpToolSpec(
                name="direct",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                annotations={"invocation_mode": "direct", "readOnlyHint": True},
            ),
            McpToolSpec(
                name="indirect",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            ),
        ),
    ).with_hash()
    projection = McpCompiler().compile(module_id="mcp_mode", snapshots=(snapshot,), invoker=invoker)
    runtime = PalCore().context.execution_runtime
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=projection.mounted_subtree))

    assert "mcp_mode_direct" in runtime.registry_generation.provider_specs
    assert "mcp_mode_indirect" not in runtime.registry_generation.provider_specs
    assert "mcp_mode_indirect" in runtime.registry_generation.indirect_aliases


@pytest.mark.parametrize(
    ("structured_content", "expected_type"),
    [({"value": 2}, CompleteResult), ({"value": "2"}, FailedResult)],
)
def test_mcp_declared_output_schema_is_validated(structured_content, expected_type) -> None:
    invoker = _Invoker(
        {"content": [{"type": "text", "text": "result"}], "structuredContent": structured_content}
    )
    snapshot = McpDiscoverySnapshot(
        server_id="output",
        transport="stdio",
        tools=(
            McpToolSpec(
                name="typed",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                output_schema={
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            ),
        ),
    ).with_hash()
    projection = McpCompiler().compile(module_id="mcp_output", snapshots=(snapshot,), invoker=invoker)
    runtime = PalCore().context.execution_runtime
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=projection.mounted_subtree))

    result = runtime.invoke_indirect_tool(new_tool_call(name="mcp_output_typed", args={}))
    assert isinstance(result, expected_type)
    if isinstance(result, FailedResult):
        assert result.error_code == "output_validation_failed"

"""External schema data must survive discovery and execution unchanged."""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Literal

import pytest
from jsonschema import Draft202012Validator

from pal.execution.tool_facade import StrictToolModel, ToolGuidance
from pal.execution.tool_semantics import INDIRECT_NONE
from pal.mcp.compiler import McpCompiler
from pal.mcp.model import McpDiscoverySnapshot, McpToolSpec
from tests.capability_fixture import mount_test_capability
from tests.test_tool_failure_affordances import invoke, metadata, runtime


SCHEMA = {
    "$defs": {"op_tool_read": {"type": "object", "properties": {
        "description": {"const": "op_tool_read"}, "title": {"const": "intro_external_literal"}},
        "required": ["description", "title"], "additionalProperties": False}},
    "type": "object",
    "description": "The value op_tool_read is external data, not a Pal routing name.",
    "properties": {
        "mode": {"const": "op_tool_read"},
        "selection": {"enum": ["op_tool_read", "intro_external_literal"]},
        "nested": {"$ref": "#/$defs/op_tool_read"},
        "op_tool_read": {"type": "string", "default": "op_tool_read", "examples": ["op_tool_read"]},
    },
    "required": ["mode", "selection", "nested", "op_tool_read"],
    "additionalProperties": False,
}
VALUE = {"mode": "op_tool_read", "selection": "intro_external_literal",
         "nested": {"description": "op_tool_read", "title": "intro_external_literal"},
         "op_tool_read": "op_tool_read"}


class Invoker:
    def __init__(self):
        self.calls = []

    def call_tool(self, server_id, tool_name, arguments):
        Draft202012Validator(SCHEMA).validate(arguments)
        self.calls.append(dict(arguments))
        return {"content": [{"type": "text", "text": "accepted"}], "structuredContent": dict(arguments)}

    def render_prompt(self, server_id, prompt_name, arguments):
        raise AssertionError("this is a tool, not a prompt")


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("direct", [False, True])
def test_mcp_input_output_and_discovery_preserve_schema_literals(runtime, asynchronous, direct):
    invoker = Invoker()
    snapshot = McpDiscoverySnapshot(server_id="literal", transport="stdio", tools=(
        McpToolSpec(name="validate", description="Accept exactly op_tool_read.",
                    input_schema=SCHEMA, output_schema=SCHEMA,
                    annotations={"invocation_mode": "direct" if direct else "indirect"}),)).with_hash()
    projection = McpCompiler().compile(module_id="mcp_literal", snapshots=(snapshot,), invoker=invoker)
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=projection.mounted_subtree))
    alias = "call_mcp_literal_validate"
    record = runtime.registry_generation.record_for_alias(alias)
    assert record.input_schema == SCHEMA
    assert record.output_schema == SCHEMA
    definition = invoke(runtime, "read_tool", {"name": alias, "view": "full"}, asynchronous=asynchronous)
    assert definition.ok, definition.llm_text
    delivered = json.loads(definition.llm_text)
    assert delivered["input_schema"] == SCHEMA
    assert delivered["output_schema"] == SCHEMA
    assert "Accept exactly op_tool_read" in delivered["description"]
    search = invoke(runtime, "search_tools", {"query": alias}, asynchronous=asynchronous)
    assert search.ok, search.llm_text
    assert search.structured["hits"][0]["input_contract"]["properties"]["mode"]["const"] == "op_tool_read"
    if direct:
        assert runtime.registry_generation.provider_specs[alias]["function"]["input_schema"] == SCHEMA

    result = invoke(runtime, alias, VALUE, asynchronous=asynchronous)
    assert result.ok, result.llm_text
    assert result.structured == VALUE
    assert invoker.calls == [VALUE]
    wrong = invoke(runtime, alias, {**VALUE, "mode": "read_tool"}, asynchronous=asynchronous)
    assert not wrong.ok
    assert metadata(wrong)["error_code"] == "invalid_arguments"
    assert invoker.calls == [VALUE]


class LiteralInput(StrictToolModel):
    value: Literal["op_tool_read", "intro_external_literal"]


def test_internal_literal_data_and_valid_example_are_not_routing_prose(runtime):
    mount_test_capability(runtime, alias="literal_identity", canonical_path="op_literal_identity",
        InputModel=LiteralInput, OutputModel=LiteralInput,
        guidance=ToolGuidance(purpose="Echo a literal value.", use_when="Testing literal data.",
                              do_not_use_when="Reading tool definitions; use op_tool_read."),
        execution=INDIRECT_NONE, examples=({"value": "op_tool_read"},),
        handler=lambda value: value)
    record = runtime.registry_generation.record_for_alias("literal_identity")
    assert record.input_schema["properties"]["value"]["enum"] == ["op_tool_read", "intro_external_literal"]
    assert "use read_tool" in record.compiled_description
    assert '"value": "op_tool_read"' in record.compiled_description
    result = invoke(runtime, "literal_identity", {"value": "op_tool_read"})
    assert result.ok, result.llm_text
    assert result.structured == {"value": "op_tool_read"}

"""MCP adapts failure envelopes while preserving successful business data."""
import json

import pytest

from pal.mcp.normalize import normalize_tool_result
from pal.shared.tool_protocol import EffectOutcome


TRACE = 'Traceback (most recent call last):\n  File "/private/server.py", line 42\n    save()\nPermissionError: denied'


@pytest.mark.parametrize("body", [
    {"error": "PermissionError: denied", "stack": TRACE},
    {"error": {"message": "wrapper failed", "cause": {"message": "PermissionError: denied"}, "stack": TRACE}},
    {"message": "PermissionError: denied", "stackTrace": TRACE},
    {"traceback": TRACE},
    {"errors": [{"reason": "PermissionError: denied", "stack": TRACE}]},
])
@pytest.mark.parametrize("encoding", ["structured", "json_text"])
def test_mcp_failure_envelopes_keep_root_and_private_evidence(body, encoding, caplog):
    raw = {"content": [], "isError": True}
    if encoding == "structured":
        raw["structuredContent"] = body
    else:
        raw["content"] = [{"type": "text", "text": json.dumps(body)}]
    result = normalize_tool_result(raw, server_id="audit", tool_name="save")
    assert result.text == "PermissionError: denied"
    assert result.llm_text == "MCP save: PermissionError: denied"
    assert "wrapper failed" not in result.user_text
    assert "Traceback" not in result.user_text
    assert result.structured["raw_result"] == raw
    assert "/private/server.py" in caplog.text
    assert result.effect_receipt.outcome == EffectOutcome.UNKNOWN
    assert "Reconcile external writes" in result.recovery_hint


def test_mcp_plain_error_redacts_secrets_in_public_text_and_logs(caplog):
    result = normalize_tool_result({"isError": True, "content": [
        {"type": "text", "text": TRACE + "; token=PRIVATE_CANARY"},
    ]}, server_id="audit", tool_name="save")
    assert "PermissionError: denied" in result.llm_text
    assert "PRIVATE_CANARY" not in result.user_text + caplog.text
    assert "Traceback" not in result.user_text


def test_mcp_unknown_error_envelope_does_not_dump_its_body():
    result = normalize_tool_result({"content": [], "isError": True,
        "structuredContent": {"internal_dump": TRACE}}, server_id="audit", tool_name="save")
    assert result.text == "Server reported a tool error"
    assert "Traceback" not in result.user_text


def test_mcp_multiple_json_content_blocks_are_decoded_separately():
    result = normalize_tool_result({"isError": True, "content": [
        {"type": "text", "text": json.dumps({"error": "denied", "stack": TRACE})},
        {"type": "text", "text": json.dumps({"error": "disconnected", "stack": TRACE})},
    ]}, server_id="audit", tool_name="save")
    assert result.text == "denied; disconnected"
    assert "Traceback" not in result.user_text


@pytest.mark.parametrize('body', [
    {'rootCause': {'message': 'denied'}, 'message': 'wrapper'},
    {'innerError': {'message': 'denied'}, 'message': 'wrapper'},
    {'stackTrace': 'Error: denied\n    at save (/private/server.js:1:2)'},
])
def test_mcp_prefixed_json_and_server_field_spellings(body):
    result = normalize_tool_result({'isError': True, 'content': [
        {'type': 'text', 'text': 'Tool execution failed: ' + json.dumps(body)},
    ]}, server_id='audit', tool_name='save')
    assert 'denied' in result.text
    assert 'wrapper' not in result.text
    assert '/private/server.js' not in result.user_text
    assert 'stackTrace' not in result.user_text


def test_mcp_structured_cause_takes_precedence_over_generic_text():
    result = normalize_tool_result({"isError": True,
        "content": [{"type": "text", "text": "Tool execution failed; see structured details"}],
        "structuredContent": {"error": {"cause": "PermissionError: denied", "message": "wrapper"}},
    }, server_id="audit", tool_name="save")
    assert result.text == "PermissionError: denied"


@pytest.mark.parametrize("transport", ["direct", "sidecar", "json_message"])
def test_mcp_json_rpc_errors_use_the_same_public_adapter(transport, caplog):
    from pal.mcp.ipc import McpManagerRpcError
    from pal.mcp.model import McpRemoteError
    from pal.mcp.normalize import normalize_protocol_error

    remote = {"code": -32603, "message": "PermissionError: denied", "data": {"stack": TRACE}}
    payload = {"raw_response": {"jsonrpc": "2.0", "id": 1, "error": remote}}
    wrapper = f"MCP request failed: tools/call: {remote}"
    if transport == "sidecar":
        error = McpManagerRpcError(wrapper, kind="remote", payload={
            "error": {"message": wrapper, "details": payload}})
    elif transport == "json_message":
        error = McpRemoteError(json.dumps(remote))
    else:
        error = McpRemoteError(wrapper, payload=payload)
    result = normalize_protocol_error(error, server_id="audit", name="save", kind="tool")
    assert result.llm_text == "MCP save: PermissionError: denied"
    assert "Traceback" not in result.user_text
    assert "/private/server.py" not in result.user_text
    assert result.effect_receipt.outcome == EffectOutcome.UNKNOWN
    assert "/private/server.py" in caplog.text


def test_mcp_success_keeps_error_named_business_fields():
    body = {"error": "sample", "stack": TRACE}
    raw = {"content": [{"type": "text", "text": json.dumps(body)}], "structuredContent": body}
    result = normalize_tool_result(raw, server_id="audit", tool_name="read")
    assert result.text == json.dumps(body)
    assert result.structured["raw_result"] == raw
    assert "/private/server.py" in result.llm_text

from __future__ import annotations

import io
from types import SimpleNamespace
import unittest

from pal.llm.contracts import CanonicalLLMRequest
from pal.llm.codex_app_server import (
    CodexAppServerAuthMessages,
    CodexAppServerClientInfo,
    is_chatgpt_auth_tokens_refresh_request,
    redact_codex_auth_message,
)
from pal.llm.codex_proxy import (
    CodexAppServerBridge,
    CodexCompletion,
    CodexProxyError,
    CodexToolCall,
    _completion_payload,
    _codex_effort_from_payload,
    _codex_env,
    _messages_to_codex_input,
    _messages_to_codex_turn,
    _models_payload,
    _parse_max_concurrency,
    _openai_tools_to_dynamic_tools,
    _parse_model_list,
    _request_authorized,
    _stream_done_payload,
    _stream_tool_call_payload,
    _strip_openai_prefix,
    _turn_start_params,
)
from pal.llm.runtime import CodexAppServerEndpointInvoker


class OpenAICodexAppServerAuthProtocolTests(unittest.TestCase):
    def test_initialize_can_advertise_experimental_api_for_external_chatgpt_tokens(self) -> None:
        messages = CodexAppServerAuthMessages(
            client_info=CodexAppServerClientInfo(name="pal-test", title="Pal Test", version="1.2.3"),
            experimental_api=True,
        )

        self.assertEqual(
            messages.initialize_request(request_id=1),
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {"name": "pal-test", "title": "Pal Test", "version": "1.2.3"},
                    "capabilities": {"experimentalApi": True},
                },
            },
        )
        self.assertEqual(messages.initialized_notification(), {"method": "initialized", "params": {}})

    def test_chatgpt_login_start_requests_match_official_auth_types(self) -> None:
        messages = CodexAppServerAuthMessages()

        self.assertEqual(
            messages.login_chatgpt_browser_request(request_id=2),
            {
                "method": "account/login/start",
                "id": 2,
                "params": {"type": "chatgpt"},
            },
        )
        self.assertEqual(
            messages.login_chatgpt_device_code_request(request_id=3),
            {
                "method": "account/login/start",
                "id": 3,
                "params": {"type": "chatgptDeviceCode"},
            },
        )
        self.assertEqual(
            messages.login_chatgpt_auth_tokens_request(
                request_id=4,
                access_token="token-a",
                chatgpt_account_id="acct_1",
                chatgpt_plan_type="pro",
            ),
            {
                "method": "account/login/start",
                "id": 4,
                "params": {
                    "type": "chatgptAuthTokens",
                    "accessToken": "token-a",
                    "chatgptAccountId": "acct_1",
                    "chatgptPlanType": "pro",
                },
            },
        )

    def test_account_read_logout_rate_limits_and_external_token_refresh_shape(self) -> None:
        messages = CodexAppServerAuthMessages()

        self.assertEqual(
            messages.account_read_request(request_id=5, refresh_token=True),
            {"method": "account/read", "id": 5, "params": {"refreshToken": True}},
        )
        self.assertEqual(messages.logout_request(request_id=6), {"method": "account/logout", "id": 6})
        self.assertEqual(messages.rate_limits_read_request(request_id=7), {"method": "account/rateLimits/read", "id": 7})

        refresh_request = {"method": "account/chatgptAuthTokens/refresh", "id": 8, "params": {}}
        self.assertTrue(is_chatgpt_auth_tokens_refresh_request(refresh_request))
        self.assertEqual(
            messages.chatgpt_auth_tokens_refresh_response(
                request_id=8,
                access_token="token-b",
                chatgpt_account_id="acct_1",
                chatgpt_plan_type="pro",
            ),
            {
                "id": 8,
                "result": {
                    "accessToken": "token-b",
                    "chatgptAccountId": "acct_1",
                    "chatgptPlanType": "pro",
                },
            },
        )

    def test_codex_auth_message_redaction_hides_tokens(self) -> None:
        raw = {
            "method": "account/login/start",
            "id": 9,
            "params": {
                "type": "chatgptAuthTokens",
                "accessToken": "token-a",
                "chatgptAccountId": "acct_1",
            },
        }

        self.assertEqual(
            redact_codex_auth_message(raw),
            {
                "method": "account/login/start",
                "id": 9,
                "params": {
                    "type": "chatgptAuthTokens",
                    "accessToken": "<redacted>",
                    "chatgptAccountId": "acct_1",
                },
            },
        )
        self.assertEqual(raw["params"]["accessToken"], "token-a")


class OpenAICodexProxyMappingTests(unittest.TestCase):
    def test_proxy_api_key_auth_accepts_openai_bearer_header(self) -> None:
        self.assertTrue(_request_authorized({"Authorization": "Bearer proxy-token"}, "proxy-token"))
        self.assertTrue(_request_authorized({"X-API-Key": "proxy-token"}, "proxy-token"))
        self.assertTrue(_request_authorized({}, ""))
        self.assertFalse(_request_authorized({}, "proxy-token"))
        self.assertFalse(_request_authorized({"Authorization": "Bearer wrong"}, "proxy-token"))

    def test_model_prefix_is_stripped_for_codex_app_server(self) -> None:
        self.assertEqual(_strip_openai_prefix("openai/gpt-5.4"), "gpt-5.4")
        self.assertEqual(_strip_openai_prefix("hosted_vllm/gpt-5.4"), "gpt-5.4")
        self.assertEqual(_strip_openai_prefix("gpt-5.3-codex"), "gpt-5.3-codex")

    def test_proxy_advertises_multiple_codex_models(self) -> None:
        model_ids = _parse_model_list("hosted_vllm/gpt-5.5,gpt-5.4\ngpt-5.3-codex,gpt-5.4")
        payload = _models_payload(model_ids)

        self.assertEqual(model_ids, ("gpt-5.5", "gpt-5.4", "gpt-5.3-codex"))
        self.assertEqual([item["id"] for item in payload["data"]], ["gpt-5.5", "gpt-5.4", "gpt-5.3-codex"])

    def test_proxy_max_concurrency_is_clamped(self) -> None:
        self.assertEqual(_parse_max_concurrency("3"), 3)
        self.assertEqual(_parse_max_concurrency("0"), 1)
        self.assertEqual(_parse_max_concurrency("99"), 32)
        self.assertEqual(_parse_max_concurrency("bad"), 3)

    def test_proxy_maps_completion_reasoning_effort_to_codex_effort(self) -> None:
        self.assertEqual(_codex_effort_from_payload({"reasoning_effort": "xhigh"}), "xhigh")
        self.assertEqual(_codex_effort_from_payload({"reasoning_effort": {"effort": "high"}}), "high")
        self.assertEqual(_codex_effort_from_payload({"reasoning": {"effort": "minimal"}}), "minimal")
        self.assertIsNone(_codex_effort_from_payload({"reasoning_effort": "invalid"}))

    def test_turn_start_params_include_codex_effort_when_present(self) -> None:
        params = _turn_start_params(thread_id="thread_1", input_text="hello", model="gpt-5.5", effort="xhigh")

        self.assertEqual(params["effort"], "xhigh")
        self.assertEqual(params["approvalPolicy"], "never")
        self.assertEqual(params["sandboxPolicy"], {"type": "readOnly"})

    def test_codex_env_keeps_symlink_parent_on_path(self) -> None:
        env = _codex_env("/tmp/node-v/bin/codex")

        self.assertEqual(env["PATH"].split(":")[0], "/tmp/node-v/bin")

    def test_messages_are_mapped_to_developer_instructions_and_turn_text(self) -> None:
        developer, turn = _messages_to_codex_turn(
            [
                {"role": "system", "content": "Use Pal policy."},
                {"role": "user", "content": "Find my reminder."},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {"name": "memory_search", "arguments": "{\"q\":\"reminder\"}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "reminder found"},
            ]
        )

        self.assertIn("Pal owns memory", developer)
        self.assertIn("Use Pal policy.", developer)
        self.assertIn("User:\nFind my reminder.", turn)
        self.assertIn("memory_search", turn)
        self.assertIn("Tool result (call_1):\nreminder found", turn)

    def test_messages_map_image_parts_to_codex_user_input(self) -> None:
        developer, input_items = _messages_to_codex_input(
            [
                {"role": "system", "content": "Use Pal policy."},
                {
                    "role": "user",
                    "content": [
                        {"type": "artifact_image", "source_url": "data:image/png;base64,abc"},
                        {"type": "text", "text": "Analyze this image."},
                    ],
                },
            ]
        )

        self.assertIn("Use Pal policy.", developer)
        self.assertEqual(
            input_items,
            [
                {"type": "text", "text": "User:\nAnalyze this image."},
                {"type": "image", "url": "data:image/png;base64,abc"},
            ],
        )

    def test_messages_map_transcript_parts_to_text_input(self) -> None:
        _developer, input_items = _messages_to_codex_input(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "transcript", "text": "Speaker A: hello\nSpeaker B: hi"},
                        {"type": "text", "text": "Summarize it."},
                    ],
                },
            ]
        )

        self.assertEqual(
            input_items,
            [{"type": "text", "text": "User:\nTranscript:\nSpeaker A: hello\nSpeaker B: hi\nSummarize it."}],
        )

    def test_openai_tools_map_to_codex_dynamic_tools(self) -> None:
        tools = _openai_tools_to_dynamic_tools(
            [
                {
                    "type": "function",
                    "function": {
                        "name": "pal_probe",
                        "description": "Probe Pal.",
                        "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                    },
                }
            ]
        )

        self.assertEqual(
            tools,
            [
                {
                    "name": "pal_probe",
                    "description": "Probe Pal.",
                    "inputSchema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
                }
            ],
        )

    def test_completion_payload_renders_openai_tool_call_shape(self) -> None:
        payload = _completion_payload(
            CodexCompletion(
                model="gpt-5.4",
                tool_call=CodexToolCall(call_id="call_1", name="pal_probe", arguments={"ok": True}),
            )
        )

        choice = payload["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertIsNone(choice["message"]["content"])
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "pal_probe")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["arguments"], "{\"ok\": true}")

    def test_stream_tool_call_payload_uses_openai_delta_shape(self) -> None:
        payload = _stream_tool_call_payload(
            "gpt-5.4",
            CodexToolCall(call_id="call_1", name="pal_probe", arguments={"ok": True}),
        )

        choice = payload["choices"][0]
        call = choice["delta"]["tool_calls"][0]
        self.assertIsNone(choice["finish_reason"])
        self.assertEqual(call["id"], "call_1")
        self.assertEqual(call["function"]["name"], "pal_probe")
        self.assertEqual(call["function"]["arguments"], "{\"ok\": true}")

    def test_stream_done_payload_sets_finish_reason(self) -> None:
        payload = _stream_done_payload("gpt-5.4", "tool_calls")

        self.assertEqual(payload["choices"][0]["delta"], {})
        self.assertEqual(payload["choices"][0]["finish_reason"], "tool_calls")

    def test_codex_bridge_waits_for_tool_call_after_agent_message_completed(self) -> None:
        class _FakeProc:
            stdin = io.StringIO()

        class _ScriptedBridge(CodexAppServerBridge):
            def __init__(self) -> None:
                super().__init__(timeout_seconds=5)
                self.items = iter(
                    [
                        {"id": 1, "result": {}},
                        {"id": 2, "result": {"thread": {"id": "thread_1"}}},
                        {"method": "item/agentMessage/delta", "params": {"delta": "I will check."}},
                        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "I will check."}}},
                        {"method": "item/tool/call", "params": {"callId": "call_1", "tool": "pal_probe", "arguments": {"ok": True}}},
                    ]
                )

            def _read_message(self, _proc, *, deadline):
                _ = deadline
                return next(self.items)

        bridge = _ScriptedBridge()
        completion = bridge._invoke_process(
            _FakeProc(),
            model="gpt-5.4",
            developer_instructions="policy",
            input_text="hello",
            input_items=None,
            dynamic_tools=[],
            effort=None,
        )

        self.assertIsNotNone(completion.tool_call)
        assert completion.tool_call is not None
        self.assertEqual(completion.tool_call.name, "pal_probe")

    def test_native_codex_invoker_returns_canonical_tool_call(self) -> None:
        class _FakeBridge:
            def __init__(self) -> None:
                self.kwargs = {}

            def invoke_turn(self, **kwargs):
                self.kwargs = dict(kwargs)
                return CodexCompletion(
                    model="gpt-5.4",
                    tool_call=CodexToolCall(call_id="call_1", name="pal_probe", arguments={"ok": True}),
                )

        endpoint = SimpleNamespace(
            endpoint_id="codex_native",
            provider="codex_proxy",
            model_id="hosted_vllm/gpt-5.4",
            base_url="http://127.0.0.1:8765/v1",
            capabilities_blob={"official_codex_app_server": True},
        )
        invoker = CodexAppServerEndpointInvoker(bridge=_FakeBridge())
        outcome = invoker.invoke(
            endpoint,
            CanonicalLLMRequest(
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "probe"},
                            {"type": "artifact_image", "source_url": "data:image/png;base64,abc"},
                        ],
                    }
                ],
                max_output_tokens=64,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "pal_probe",
                            "description": "Probe Pal.",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
                metadata={"think_level": "deep"},
            ),
        )

        self.assertEqual(outcome.finish_reason, "tool_calls")
        self.assertEqual(outcome.tool_calls[0].name, "pal_probe")
        self.assertEqual(outcome.tool_calls[0].args, {"ok": True})
        self.assertEqual(invoker.bridge.kwargs["model"], "gpt-5.4")
        self.assertEqual(invoker.bridge.kwargs["effort"], "high")
        self.assertEqual(
            invoker.bridge.kwargs["input_items"],
            [
                {"type": "text", "text": "User:\nprobe"},
                {"type": "image", "url": "data:image/png;base64,abc"},
            ],
        )
        self.assertEqual(invoker.bridge.kwargs["dynamic_tools"][0]["name"], "pal_probe")

    def test_native_codex_invoker_retries_final_answer_without_tools_after_tool_timeout(self) -> None:
        class _FakeBridge:
            def __init__(self) -> None:
                self.calls = []

            def invoke_turn(self, **kwargs):
                self.calls.append(dict(kwargs))
                if kwargs["dynamic_tools"]:
                    raise CodexProxyError("timed out waiting for Codex app-server output")
                return CodexCompletion(model="gpt-5.4", text="DONE")

        endpoint = SimpleNamespace(
            endpoint_id="codex_native",
            provider="codex_app_server",
            model_id="hosted_vllm/gpt-5.4",
            base_url="codex://app-server",
            capabilities_blob={"official_codex_app_server": True},
        )
        bridge = _FakeBridge()
        invoker = CodexAppServerEndpointInvoker(bridge=bridge)
        outcome = invoker.invoke(
            endpoint,
            CanonicalLLMRequest(
                messages=[
                    {"role": "user", "content": "probe"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "pal_probe", "arguments": "{}"},
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "probe result"},
                ],
                max_output_tokens=64,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "pal_probe",
                            "description": "Probe Pal.",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            ),
        )

        self.assertEqual(outcome.text, "DONE")
        self.assertEqual(len(bridge.calls), 2)
        self.assertEqual(bridge.calls[0]["dynamic_tools"][0]["name"], "pal_probe")
        self.assertEqual(bridge.calls[1]["dynamic_tools"], [])
        self.assertIn("Produce the final user-facing answer now", bridge.calls[1]["developer_instructions"])

    def test_native_codex_invoker_finalizes_instead_of_repeating_completed_tool_call(self) -> None:
        class _FakeBridge:
            def __init__(self) -> None:
                self.calls = []

            def invoke_turn(self, **kwargs):
                self.calls.append(dict(kwargs))
                if kwargs["dynamic_tools"]:
                    return CodexCompletion(
                        model="gpt-5.4",
                        tool_call=CodexToolCall(
                            call_id="call_2",
                            name="pal_probe",
                            arguments={"step": "one"},
                        ),
                    )
                return CodexCompletion(model="gpt-5.4", text="DONE")

        endpoint = SimpleNamespace(
            endpoint_id="codex_native",
            provider="codex_app_server",
            model_id="hosted_vllm/gpt-5.4",
            base_url="codex://app-server",
            capabilities_blob={"official_codex_app_server": True},
        )
        bridge = _FakeBridge()
        invoker = CodexAppServerEndpointInvoker(bridge=bridge)
        outcome = invoker.invoke(
            endpoint,
            CanonicalLLMRequest(
                messages=[
                    {"role": "user", "content": "probe"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "pal_probe",
                                    "arguments": "{\"step\": \"one\"}",
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "probe result"},
                ],
                max_output_tokens=64,
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "pal_probe",
                            "description": "Probe Pal.",
                            "parameters": {"type": "object"},
                        },
                    }
                ],
            ),
        )

        self.assertEqual(outcome.text, "DONE")
        self.assertEqual(len(bridge.calls), 2)
        self.assertEqual(bridge.calls[1]["dynamic_tools"], [])


if __name__ == "__main__":
    unittest.main()

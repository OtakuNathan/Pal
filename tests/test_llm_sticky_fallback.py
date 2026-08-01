from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.core import PalCore
from pal.execution import CapabilityCall, register_with_core as register_execution_with_core
from pal.llm import EndpointResolver, LLMRuntime, RuntimeSettingRepository
from pal.llm.contracts import generation_result_from_values, request_ir_from_prompt
from pal.llm.capabilities import LLMIntrospectionProvider, register_with_core as register_llm_with_core
from pal.runtime_app import open_runtime
from pal.shared import LLMFinishReason


class _FailoverInvoker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(self, endpoint, request, **kwargs):
        _ = kwargs
        self.calls.append(endpoint.endpoint_id)
        if endpoint.endpoint_id == "broken":
            raise RuntimeError("broken endpoint")
        return generation_result_from_values(text=f"ok:{endpoint.endpoint_id}").response, ()

    def invoke_stream(self, endpoint, request):
        raise NotImplementedError


def _fake_endpoint(endpoint_id: str, model_id: str):
    return SimpleNamespace(
        endpoint_id=endpoint_id,
        model_id=model_id,
        provider="openai",
        display_name=model_id,
        wire_shape="openai_completion",
        base_url="https://example.test/v1",
        auth_kind="api_key_ref",
        credential_ref=f"{endpoint_id.upper()}_API_KEY",
        capabilities_blob={},
        thinking_levels_blob=["off"],
        default_thinking_level="off",
        supports_tools=True,
        supports_streaming=False,
        supports_vision=False,
        max_output_tokens=1024,
        context_window=8192,
        input_modalities_blob=[],
        output_modalities_blob=[],
        priority=0,
        enabled=True,
        notes=None,
    )


class _MemorySettingsRepository:
    def __init__(self) -> None:
        self.think_levels: dict[str, str] = {}
        self.active_endpoint_id: str | None = None

    def get_think_level(self, endpoint_id: str) -> str | None:
        return self.think_levels.get(endpoint_id)

    def set_think_level(self, endpoint_id: str, think_level: str) -> None:
        self.think_levels[endpoint_id] = str(think_level)

    def get_legacy_think_level(self) -> str | None:
        return None

    def delete_legacy_think_level(self) -> bool:
        return False

    def get_active_llm_endpoint_id(self) -> str | None:
        return self.active_endpoint_id

    def set_active_llm_endpoint_id(self, endpoint_id: str) -> None:
        self.active_endpoint_id = str(endpoint_id)


class PalV2LLMStickyFallbackTests(unittest.TestCase):
    def test_fallback_sticks_to_working_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handle = open_runtime(Path(tmpdir))
            try:
                broken = _fake_endpoint("broken", "broken-model")
                working = _fake_endpoint("working", "working-model")
                invoker = _FailoverInvoker()
                runtime = LLMRuntime(
                    endpoint_resolver=EndpointResolver(endpoints=(broken, working)),
                    settings_repository=RuntimeSettingRepository(),
                    endpoint_invoker=invoker,
                )
                request = request_ir_from_prompt(messages=[{"role": "user", "content": "hi"}], max_output_tokens=64)

                first = runtime.generate(request)
                second = runtime.generate(request)

                self.assertEqual(first.text, "ok:working")
                self.assertEqual(second.text, "ok:working")
                self.assertEqual(invoker.calls, ["broken", "broken", "broken", "working", "working"])
                self.assertEqual(runtime.active_endpoint_id, "working")
                self.assertEqual(RuntimeSettingRepository().get_active_llm_endpoint_id(), "working")
            finally:
                asyncio.run(handle.stop_async())

    def test_profile_preferred_missing_does_not_fall_back_to_active_endpoint(self) -> None:
        settings = _MemorySettingsRepository()
        settings.active_endpoint_id = "active"
        active = _fake_endpoint("active", "active-model")
        other = _fake_endpoint("other", "other-model")
        invoker = _FailoverInvoker()
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=(other, active)),
            settings_repository=settings,
            endpoint_invoker=invoker,
        )

        outcome = runtime.generate(
            request_ir_from_prompt(
                messages=[{"role": "user", "content": "hi"}],
                max_output_tokens=64,
                metadata={"preferred_endpoint_id": "missing", "preferred_endpoint_source": "profile"},
            )
        )

        self.assertEqual(outcome.finish_reason, LLMFinishReason.ERROR)
        self.assertIn("no enabled endpoints", outcome.text)
        self.assertEqual(invoker.calls, [])
        self.assertEqual(runtime.active_endpoint_id, "active")

    def test_profile_preferred_failure_does_not_fall_back_to_active_endpoint(self) -> None:
        settings = _MemorySettingsRepository()
        settings.active_endpoint_id = "active"
        active = _fake_endpoint("active", "active-model")
        broken = _fake_endpoint("broken", "broken-model")
        other = _fake_endpoint("other", "other-model")
        invoker = _FailoverInvoker()
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=(active, broken, other)),
            settings_repository=settings,
            endpoint_invoker=invoker,
            endpoint_retry_attempts=1,
        )

        outcome = runtime.generate(
            request_ir_from_prompt(
                messages=[{"role": "user", "content": "hi"}],
                max_output_tokens=64,
                metadata={"preferred_endpoint_id": "broken", "preferred_endpoint_source": "profile"},
            )
        )

        self.assertEqual(outcome.finish_reason, LLMFinishReason.ERROR)
        self.assertIn("kind=unknown", outcome.text)
        self.assertIn("type=RuntimeError", outcome.text)
        self.assertNotIn("broken endpoint", outcome.text)
        self.assertEqual(invoker.calls, ["broken"])
        self.assertEqual(runtime.active_endpoint_id, "active")

    def test_profile_preferred_missing_does_not_resolve_budget_from_active_endpoint(self) -> None:
        settings = _MemorySettingsRepository()
        settings.active_endpoint_id = "active"
        active = _fake_endpoint("active", "active-model")
        active.max_output_tokens = 777
        other = _fake_endpoint("other", "other-model")
        other.max_output_tokens = 555
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=(other, active)),
            settings_repository=settings,
            endpoint_invoker=_FailoverInvoker(),
        )

        self.assertEqual(
            runtime.resolve_max_output_tokens(preferred_endpoint_id="missing", preferred_endpoint_source="profile"),
            None,
        )
        facts = runtime.resolve_endpoint_facts(preferred_endpoint_id="missing", preferred_endpoint_source="profile")
        self.assertEqual(facts["endpoint_id"], "missing")
        self.assertIsNone(facts["max_output_tokens"])

    def test_endpoint_fallback_policy_none_uses_only_selected_endpoint(self) -> None:
        settings = _MemorySettingsRepository()
        settings.active_endpoint_id = "broken"
        broken = _fake_endpoint("broken", "broken-model")
        working = _fake_endpoint("working", "working-model")
        invoker = _FailoverInvoker()
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=(broken, working)),
            settings_repository=settings,
            endpoint_invoker=invoker,
            endpoint_retry_attempts=1,
        )

        outcome = runtime.generate(
            request_ir_from_prompt(
                messages=[{"role": "user", "content": "hi"}],
                max_output_tokens=64,
                metadata={"endpoint_fallback_policy": "none"},
            )
        )

        self.assertEqual(outcome.finish_reason, LLMFinishReason.ERROR)
        self.assertIn("kind=unknown", outcome.text)
        self.assertIn("type=RuntimeError", outcome.text)
        self.assertNotIn("broken endpoint", outcome.text)
        self.assertEqual(invoker.calls, ["broken"])

    def test_timeout_error_exhausts_endpoint_and_falls_back(self) -> None:
        class _Invoker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def invoke(self, endpoint, request, **kwargs):
                _ = request, kwargs
                self.calls.append(endpoint.endpoint_id)
                if endpoint.endpoint_id == "slow":
                    raise TimeoutError("timed out waiting for provider response")
                return generation_result_from_values(text=f"ok:{endpoint.endpoint_id}").response, ()

            def invoke_stream(self, endpoint, request):
                raise NotImplementedError

        slow = _fake_endpoint("slow", "slow-model")
        working = _fake_endpoint("working", "working-model")
        invoker = _Invoker()
        events: list[dict[str, object]] = []
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=(slow, working)),
            settings_repository=_MemorySettingsRepository(),
            endpoint_invoker=invoker,
            endpoint_retry_attempts=3,
            event_sink=events.append,
        )

        outcome = runtime.generate(request_ir_from_prompt(messages=[{"role": "user", "content": "hi"}], max_output_tokens=64))

        self.assertEqual(outcome.text, "ok:working")
        self.assertEqual(invoker.calls, ["slow", "slow", "slow", "working"])
        self.assertEqual(events[0]["phase"], "llm_endpoint_attempt_failed")
        self.assertEqual(events[0]["error_kind"], "timeout")
        self.assertEqual(events[3]["phase"], "llm_endpoint_exhausted")
        self.assertEqual(events[3]["reason"], "timeout")
        self.assertIn("llm_endpoint_fallback_succeeded", [event["phase"] for event in events])

    def test_bad_request_skips_identical_retry_and_falls_back(self) -> None:
        class _Invoker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def invoke(self, endpoint, request, **kwargs):
                _ = request, kwargs
                self.calls.append(endpoint.endpoint_id)
                if endpoint.endpoint_id == "malformed":
                    raise RuntimeError("Error code: 400 - invalid tool continuation")
                return generation_result_from_values(text=f"ok:{endpoint.endpoint_id}").response, ()

            def invoke_stream(self, endpoint, request):
                raise NotImplementedError

        malformed = _fake_endpoint("malformed", "deepseek-v4-flash")
        working = _fake_endpoint("working", "working-model")
        invoker = _Invoker()
        events: list[dict[str, object]] = []
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=(malformed, working)),
            settings_repository=_MemorySettingsRepository(),
            endpoint_invoker=invoker,
            endpoint_retry_attempts=3,
            event_sink=events.append,
        )

        outcome = runtime.generate(
            request_ir_from_prompt(
                messages=[{"role": "user", "content": "hi"}],
                max_output_tokens=64,
            )
        )

        self.assertEqual(outcome.text, "ok:working")
        self.assertEqual(invoker.calls, ["malformed", "working"])
        self.assertEqual(events[0]["error_kind"], "bad_request")
        self.assertEqual(events[1]["phase"], "llm_endpoint_exhausted")
        self.assertEqual(events[1]["reason"], "bad_request")

    def test_agenerate_falls_back_when_primary_blocks_past_attempt_timeout(self) -> None:
        class _BlockingInvoker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def invoke(self, endpoint, request, **kwargs):
                _ = request, kwargs
                self.calls.append(endpoint.endpoint_id)
                if endpoint.endpoint_id == "blocked":
                    raise TimeoutError("provider exceeded configured timeout")
                return generation_result_from_values(text=f"ok:{endpoint.endpoint_id}").response, ()

            def invoke_stream(self, endpoint, request):
                raise NotImplementedError

        blocked = _fake_endpoint("blocked", "blocked-model")
        working = _fake_endpoint("working", "working-model")
        invoker = _BlockingInvoker()
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=(blocked, working)),
            settings_repository=_MemorySettingsRepository(),
            endpoint_invoker=invoker,
            endpoint_retry_attempts=1,
        )
        request = request_ir_from_prompt(
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=64,
            metadata={"timeout_seconds": 1},
        )

        outcome = asyncio.run(runtime.agenerate(request))

        self.assertEqual(outcome.text, "ok:working")
        self.assertEqual(invoker.calls[:2], ["blocked", "working"])

    def test_set_active_endpoint_switches_runtime_preference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handle = open_runtime(Path(tmpdir))
            try:
                alpha = _fake_endpoint("alpha", "alpha-model")
                beta = _fake_endpoint("beta", "beta-model")
                runtime = LLMRuntime(
                    endpoint_resolver=EndpointResolver(endpoints=(alpha, beta)),
                    settings_repository=RuntimeSettingRepository(),
                    endpoint_invoker=_FailoverInvoker(),
                )
                provider = LLMIntrospectionProvider(runtime=runtime)
                result = provider.set_active_endpoint(type("Call", (), {"args": {"active_endpoint_id": "beta"}})())

                self.assertEqual(result.status, "ok")
                self.assertEqual(result.structured["active_endpoint_id"], "beta")
                self.assertEqual(result.structured["endpoint_id"], "beta")
                self.assertEqual(RuntimeSettingRepository().get_active_llm_endpoint_id(), "beta")
            finally:
                asyncio.run(handle.stop_async())

    def test_set_active_endpoint_is_discoverable_and_callable_by_llm(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            handle = open_runtime(Path(tmpdir))
            try:
                alpha = _fake_endpoint("alpha", "alpha-model")
                beta = _fake_endpoint("beta", "beta-model")
                runtime = LLMRuntime(
                    endpoint_resolver=EndpointResolver(endpoints=(alpha, beta)),
                    settings_repository=RuntimeSettingRepository(),
                    endpoint_invoker=_FailoverInvoker(),
                )
                core = PalCore()
                register_execution_with_core(core.context)
                core.publish_module_capabilities("execution")
                register_llm_with_core(core.context, runtime)
                core.publish_module_capabilities("llm")

                search = core.context.execution_runtime.execute(
                    CapabilityCall(name="op_tool_search", args={"query": "active llm endpoint", "top_k": 10})
                )
                self.assertEqual(search.status, "ok")
                hit_names = [item["alias"] for item in search.structured["hits"]]
                self.assertIn("llm_set_active_endpoint", hit_names)

                read = core.context.execution_runtime.execute(
                    CapabilityCall(name="op_tool_read", args={"name": "llm_set_active_endpoint"})
                )
                self.assertEqual(read.status, "ok")
                self.assertEqual(read.structured["input_schema"]["required"], ["active_endpoint_id"])

                call = core.context.execution_runtime.execute(
                    CapabilityCall(
                        name="op_tool_call",
                        args={"name": "llm_set_active_endpoint", "args": {"active_endpoint_id": "beta"}},
                    )
                )
                self.assertEqual(call.status, "ok")
                self.assertEqual(call.structured["payload"]["active_endpoint_id"], "beta")
                self.assertEqual(RuntimeSettingRepository().get_active_llm_endpoint_id(), "beta")
            finally:
                asyncio.run(handle.stop_async())


if __name__ == "__main__":
    unittest.main()

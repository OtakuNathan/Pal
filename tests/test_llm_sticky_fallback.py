from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.core import PalCore
from pal.execution import CapabilityCall, register_with_core as register_execution_with_core
from pal.llm import EndpointResolver, LLMRuntime, RuntimeSettingRepository
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest
from pal.llm.introspection import LLMIntrospectionProvider, register_with_core as register_llm_with_core
from pal.runtime_app import open_runtime


class _FailoverInvoker:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def invoke(self, endpoint, request):
        self.calls.append(endpoint.endpoint_id)
        if endpoint.endpoint_id == "broken":
            raise RuntimeError("broken endpoint")
        return CanonicalLLMOutcome(text=f"ok:{endpoint.endpoint_id}")

    def invoke_stream(self, endpoint, request):
        raise NotImplementedError


def _fake_endpoint(endpoint_id: str, model_id: str):
    return SimpleNamespace(
        endpoint_id=endpoint_id,
        model_id=model_id,
        provider="openai",
        base_url="",
        capabilities_blob={},
        supports_streaming=False,
        max_output_tokens=1024,
        context_window=8192,
    )


class _MemorySettingsRepository:
    def __init__(self) -> None:
        self.think_level = "balanced"
        self.active_endpoint_id: str | None = None

    def get_think_level(self) -> str:
        return self.think_level

    def set_think_level(self, think_level: str) -> None:
        self.think_level = str(think_level)

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
                request = CanonicalLLMRequest(messages=[{"role": "user", "content": "hi"}], max_output_tokens=64)

                first = runtime.generate(request)
                second = runtime.generate(request)

                self.assertEqual(first.text, "ok:working")
                self.assertEqual(second.text, "ok:working")
                self.assertEqual(invoker.calls, ["broken", "broken", "broken", "working", "working"])
                self.assertEqual(runtime.active_endpoint_id, "working")
                self.assertEqual(RuntimeSettingRepository().get_active_llm_endpoint_id(), "working")
            finally:
                asyncio.run(handle.stop_async())

    def test_codex_cli_connection_failure_skips_same_failure_domain(self) -> None:
        class _Invoker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def invoke(self, endpoint, request):
                _ = request
                self.calls.append(endpoint.endpoint_id)
                if str(endpoint.provider) == "codex_cli":
                    raise TimeoutError("timed out waiting for codex app-server output")
                return CanonicalLLMOutcome(text=f"ok:{endpoint.endpoint_id}")

            def invoke_stream(self, endpoint, request):
                raise NotImplementedError

        with tempfile.TemporaryDirectory() as tmpdir:
            handle = open_runtime(Path(tmpdir))
            try:
                codex_a = SimpleNamespace(
                    endpoint_id="codex_a",
                    model_id="gpt-5.4",
                    provider="codex_cli",
                    base_url="codex://cli",
                    capabilities_blob={"official_codex_cli": True},
                    supports_streaming=False,
                    max_output_tokens=1024,
                    context_window=8192,
                )
                codex_b = SimpleNamespace(
                    endpoint_id="codex_b",
                    model_id="gpt-5.3-codex-spark",
                    provider="codex_cli",
                    base_url="codex://cli",
                    capabilities_blob={"official_codex_cli": True},
                    supports_streaming=False,
                    max_output_tokens=1024,
                    context_window=8192,
                )
                glm = _fake_endpoint("glm", "glm-5.1")
                glm.provider = "zhipu"
                invoker = _Invoker()
                runtime = LLMRuntime(
                    endpoint_resolver=EndpointResolver(endpoints=(codex_a, codex_b, glm)),
                    settings_repository=RuntimeSettingRepository(),
                    endpoint_invoker=invoker,
                )

                outcome = runtime.generate(CanonicalLLMRequest(messages=[{"role": "user", "content": "hi"}], max_output_tokens=64))

                self.assertEqual(outcome.text, "ok:glm")
                self.assertEqual(invoker.calls, ["codex_a", "glm"])
            finally:
                asyncio.run(handle.stop_async())

    def test_timeout_error_exhausts_endpoint_and_falls_back(self) -> None:
        class _Invoker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def invoke(self, endpoint, request):
                _ = request
                self.calls.append(endpoint.endpoint_id)
                if endpoint.endpoint_id == "slow":
                    raise TimeoutError("timed out waiting for provider response")
                return CanonicalLLMOutcome(text=f"ok:{endpoint.endpoint_id}")

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

        outcome = runtime.generate(CanonicalLLMRequest(messages=[{"role": "user", "content": "hi"}], max_output_tokens=64))

        self.assertEqual(outcome.text, "ok:working")
        self.assertEqual(invoker.calls, ["slow", "working"])
        self.assertEqual(events[0]["phase"], "llm_endpoint_attempt_failed")
        self.assertEqual(events[0]["error_kind"], "timeout")
        self.assertEqual(events[1]["phase"], "llm_endpoint_exhausted")
        self.assertEqual(events[1]["reason"], "timeout")
        self.assertIn("llm_endpoint_fallback_succeeded", [event["phase"] for event in events])

    def test_agenerate_falls_back_when_primary_blocks_past_attempt_timeout(self) -> None:
        class _BlockingInvoker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def invoke(self, endpoint, request):
                _ = request
                self.calls.append(endpoint.endpoint_id)
                if endpoint.endpoint_id == "blocked":
                    time.sleep(2.0)
                    return CanonicalLLMOutcome(text="late blocked reply")
                return CanonicalLLMOutcome(text=f"ok:{endpoint.endpoint_id}")

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
        request = CanonicalLLMRequest(
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
                hit_names = [item["name"] for item in search.structured["hits"]]
                self.assertIn("op_llm_mgmt_set_active_endpoint", hit_names)

                read = core.context.execution_runtime.execute(
                    CapabilityCall(name="op_tool_read", args={"name": "op_llm_mgmt_set_active_endpoint"})
                )
                self.assertEqual(read.status, "ok")
                self.assertEqual(read.structured["capability"]["required_params"], ["active_endpoint_id"])

                call = core.context.execution_runtime.execute(
                    CapabilityCall(
                        name="op_tool_call",
                        args={"name": "op_llm_mgmt_set_active_endpoint", "args": {"active_endpoint_id": "beta"}},
                    )
                )
                self.assertEqual(call.status, "ok")
                self.assertEqual(call.structured["active_endpoint_id"], "beta")
                self.assertEqual(RuntimeSettingRepository().get_active_llm_endpoint_id(), "beta")
            finally:
                asyncio.run(handle.stop_async())


if __name__ == "__main__":
    unittest.main()

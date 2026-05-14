from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.llm import EndpointResolver, LLMRuntime, RuntimeSettingRepository
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest
from pal.llm.introspection import LLMIntrospectionProvider
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
                self.assertEqual(invoker.calls, ["broken", "broken", "working", "working"])
                self.assertEqual(runtime.active_endpoint_id, "working")
                self.assertEqual(RuntimeSettingRepository().get_active_llm_endpoint_id(), "working")
            finally:
                asyncio.run(handle.stop_async())

    def test_codex_app_server_connection_failure_skips_same_failure_domain(self) -> None:
        class _Invoker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def invoke(self, endpoint, request):
                _ = request
                self.calls.append(endpoint.endpoint_id)
                if str(endpoint.provider) == "codex_app_server":
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
                    provider="codex_app_server",
                    base_url="codex://app-server",
                    capabilities_blob={"official_codex_app_server": True},
                    supports_streaming=False,
                    max_output_tokens=1024,
                    context_window=8192,
                )
                codex_b = SimpleNamespace(
                    endpoint_id="codex_b",
                    model_id="gpt-5.3-codex-spark",
                    provider="codex_app_server",
                    base_url="codex://app-server",
                    capabilities_blob={"official_codex_app_server": True},
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


if __name__ == "__main__":
    unittest.main()

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

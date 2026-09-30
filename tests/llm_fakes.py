"""Explicit minimal LLM test implementation; individual tests supply outcomes."""
from __future__ import annotations

from pal.llm.contracts import LLMPreflightAdvice


class NonStreamingLLM:
    projection_port = None
    last_endpoint_id = None
    last_model_id = None
    last_projection_receipt = None

    def supports_streaming(self, request=None):
        return False

    def resolve_endpoint_facts(self, *, preferred_endpoint_id=None, preferred_endpoint_source=None):
        return {"supports_streaming": self.supports_streaming()}

    def resolve_max_output_tokens(self, *, preferred_endpoint_id=None, preferred_endpoint_source=None):
        return None

    def prompt_cache_eligible_anchor_request(self, *, logical_scope_id="pal:resident", endpoint_id=""):
        return self.prompt_cache_confirmed_anchor_request(logical_scope_id=logical_scope_id, endpoint_id=endpoint_id)

    def prompt_cache_confirmed_anchor_request(self, *, logical_scope_id="pal:resident", endpoint_id=""):
        return {}

    def preflight(self, request):
        return LLMPreflightAdvice(status="ready")

    async def apreflight(self, request):
        return self.preflight(request)

    def generate(self, request, **options):
        raise AssertionError("test must supply its generation result")

    async def agenerate(self, request, **options):
        return self.generate(request, **options)

    async def astream(self, request, **options):
        raise AssertionError("non-streaming fixture cannot be streamed")
        yield

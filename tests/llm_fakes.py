"""Explicit minimal LLM test implementation; individual tests supply outcomes."""
from __future__ import annotations

from pal.llm.contracts import LLMPreflightAdvice


class NonStreamingLLM:
    projection_port = None
    last_endpoint_id = None
    last_model_id = None
    last_projection_receipt = None

    def plan_compaction(self, endpoint_id=None, *, force_semantic=False):
        from pal.llm.contracts import LLMCompactionPlan
        return LLMCompactionPlan()

    def release_context(self, context_id):
        pass

    def supports_streaming(self, request=None):
        return False

    def metadata(self, *, preferred_endpoint_id=None, preferred_endpoint_source=None):
        from pal.llm.contracts import LLMMetadata
        return LLMMetadata(**self._endpoint_facts(preferred_endpoint_id=preferred_endpoint_id,
            preferred_endpoint_source=preferred_endpoint_source))

    def _endpoint_facts(self, *, preferred_endpoint_id=None, preferred_endpoint_source=None):
        return {"supports_streaming": self.supports_streaming()}

    def resolve_max_output_tokens(self, *, preferred_endpoint_id=None, preferred_endpoint_source=None):
        return None

    def prompt_cache_eligible_anchor_request(self, *, logical_scope_id="pal:resident", endpoint_id=""):
        return self.prompt_cache_confirmed_anchor_request(logical_scope_id=logical_scope_id, endpoint_id=endpoint_id)

    def prompt_cache_confirmed_anchor_request(self, *, logical_scope_id="pal:resident", endpoint_id=""):
        return {}

    def _preflight_response(self, request):
        return LLMPreflightAdvice(status="ready")

    async def preflight(self, request):
        return self._preflight_response(request)

    def _generate_response(self, request, **options):
        raise AssertionError("test must supply its generation result")

    async def respond(self, request, **options):
        return self._generate_response(request, **options)

    async def generate(self, request, **options):
        from pal.llm.ir import LLMResponseUpdate, LLMResponseDeltaKind
        result = await self.respond(request, **options)
        yield LLMResponseUpdate(result.response, delta_kind=LLMResponseDeltaKind.STATE, completion=result)


def scripted_llm(**methods):
    runtime = NonStreamingLLM()
    for name, method in methods.items():
        setattr(runtime, name, method)
    return runtime

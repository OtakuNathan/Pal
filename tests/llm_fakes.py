"""Explicit minimal LLM test implementation; individual tests supply outcomes."""
from __future__ import annotations
from contextlib import contextmanager

from pal.llm.contracts import LLMPreflightAdvice


class MemoryRuntimeSettings:
    def __init__(self, active_endpoint_id):
        self.values = {'active_llm_endpoint_id': active_endpoint_id}

    def get(self, setting_key):
        return self.values.get(setting_key)

    def set(self, setting_key, setting_value):
        self.values[setting_key] = setting_value
        return setting_value

    @contextmanager
    def selection_transaction(self):
        previous = dict(self.values)
        try:
            yield
        except BaseException:
            self.values = previous
            raise

    def get_active_llm_endpoint_id(self):
        return self.get('active_llm_endpoint_id')

    def set_active_llm_endpoint_id(self, endpoint_id):
        return self.set('active_llm_endpoint_id', endpoint_id)

    def get_think_level(self, endpoint_id):
        return self.get(f'think_level:{endpoint_id}') or 'off'

    def set_think_level(self, endpoint_id, think_level):
        return self.set(f'think_level:{endpoint_id}', think_level)


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

    def end_prompt_cache_turn(self, turn_id):
        pass

    def prompt_cache_keepalive_snapshot(self):
        return {}

    async def keepalive_once(self, epoch):
        return False

    def clear_keepalive(self):
        pass

    def claim_keepalive_compact(self, epoch):
        return False

    def keepalive_compact_valid(self, epoch):
        return False

    def prompt_cache_reminder_snapshot(self):
        return {}

    def prompt_cache_warm_deadline_snapshot(self):
        return {}

    def supports_streaming(self, request=None):
        return False

    def metadata(self, *, preferred_endpoint_id=None, preferred_endpoint_source=None):
        from pal.llm.contracts import LLMMetadata
        return LLMMetadata(**self._endpoint_facts(preferred_endpoint_id=preferred_endpoint_id,
            preferred_endpoint_source=preferred_endpoint_source))

    def _endpoint_facts(self, *, preferred_endpoint_id=None, preferred_endpoint_source=None):
        return {"supports_streaming": self.supports_streaming()}

    def snapshot_turn_settings(self):
        from pal.llm.contracts import LLMTurnSettings
        return LLMTurnSettings()

    def resolve_output_budget(self, *, preferred_endpoint_id=None, preferred_endpoint_source=None, requested=None):
        limit = self.resolve_max_output_tokens(preferred_endpoint_id=preferred_endpoint_id,
            preferred_endpoint_source=preferred_endpoint_source)
        if requested is not None:
            return min(requested, limit) if limit is not None else requested
        return limit if limit is not None else 4096

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

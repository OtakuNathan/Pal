from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

from pal.llm.ir import (
    LLMRequestIR,
    LLMResponseIR,
    LLMResponseUpdate,
    WireShape,
)
from pal.llm.attempts import LLMAttemptResult
from pal.llm.response_evidence import WireResponseEvidence
from pal.llm.models import LLMEndpointModel
from pal.llm.prompt_cache import PromptCacheCoordinator
from pal.llm.response_hooks import ProviderResponseHookRegistry
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.llm.transport import (
    DirectSDKTransport,
    EncodedTransportRequest,
    LLMJSONTransportPort,
    LLMStreamControl,
    LLMStreamCancelledError,
    SDKJSONTransport,
)
from pal.shared import LLMFinishReason


CredentialResolver = Callable[[LLMEndpointModel], str | None]


@dataclass
class ShapeEndpointInvoker:
    # credential_resolver remains as a constructor compatibility shim. New
    # runtimes inject a complete transport and keep credentials below it.
    credential_resolver: CredentialResolver | None = None
    transport: LLMJSONTransportPort | Any | None = None
    response_hooks: ProviderResponseHookRegistry = field(
        default_factory=ProviderResponseHookRegistry.builtin
    )
    prompt_cache: PromptCacheCoordinator = field(default_factory=PromptCacheCoordinator)
    attempt_sink: Callable[[LLMAttemptResult], Any] | None = None

    def __post_init__(self) -> None:
        if isinstance(self.transport, DirectSDKTransport):
            return
        if self.credential_resolver is not None:
            self.transport = DirectSDKTransport(
                credential_resolver=self.credential_resolver,
                sdk_transport=self.transport or SDKJSONTransport(),
            )
            return
        if self.transport is None:
            raise TypeError("ShapeEndpointInvoker requires an LLM JSON transport")

    def refresh_credentials(self) -> bool:
        refresh = getattr(self.transport, "refresh_credentials", None)
        return bool(refresh() if callable(refresh) else False)

    def activate_endpoint(self, endpoint_id: str) -> None:
        activate = getattr(self.transport, "activate_endpoint", None)
        if callable(activate):
            activate(endpoint_id)

    def close(self) -> None:
        close = getattr(self.transport, "close", None)
        if callable(close):
            close()

    def invoke(
        self, endpoint: LLMEndpointModel, request: LLMRequestIR, *,
        stream: bool = False, timeout_seconds: float = 600.0,
    ) -> tuple[LLMResponseIR, tuple[LLMResponseUpdate, ...]]:
        updates = tuple(self._iterate(endpoint, request, stream=stream, timeout_seconds=timeout_seconds))
        return updates[-1].response, updates

    def invoke_updates(
        self, endpoint: LLMEndpointModel, request: LLMRequestIR, *,
        timeout_seconds: float = 600.0, stream_control: LLMStreamControl | None = None,
    ) -> Iterator[LLMResponseUpdate]:
        yield from self._iterate(endpoint, request, stream=True,
                                 timeout_seconds=timeout_seconds, stream_control=stream_control)

    def _iterate(
        self, endpoint: LLMEndpointModel, request: LLMRequestIR, *,
        stream: bool, timeout_seconds: float, stream_control: LLMStreamControl | None = None,
    ) -> Iterator[LLMResponseUpdate]:
        shape = WireShape(str(endpoint.wire_shape))
        capabilities = dict(endpoint.capabilities_blob or {})
        # Runtime installs the validated turn snapshot; direct invoker callers
        # still resolve trusted endpoint capabilities.
        selection = request.metadata.get("cache_policy_selection")
        if selection is not None:
            capabilities["prompt_cache"] = dict(selection)
        context = ShapeContext(
            wire_shape=shape, endpoint_id=str(endpoint.endpoint_id),
            model_id=str(endpoint.model_id), provider_id=str(endpoint.provider),
            base_url=str(endpoint.base_url or ""), capabilities=capabilities,
        )
        codec = codec_for_shape(shape)
        raw_encoded = codec.encode(request, context)
        plan = self.prompt_cache.plan(request, context, raw_encoded)
        encoded = self.prompt_cache.inject(raw_encoded, plan)
        request_id = f"llm_{uuid4().hex}"
        started_at = time.monotonic()
        evidence = WireResponseEvidence(shape)
        diagnostics = self.prompt_cache.start_attempt(
            plan, request=request, context=context, encoded=encoded,
            raw_encoded=raw_encoded, request_id=request_id,
        )
        transport_request = EncodedTransportRequest(
            request_id=request_id, wire_shape=shape, timeout_seconds=float(timeout_seconds),
            payload=encoded.payload, extra_body=encoded.extra_body,
            stream=stream, stream_control=stream_control,
        )
        status, error_type = "failed", ""
        last: LLMResponseUpdate | None = None
        frames = None
        decoded = None

        def observed_frames():
            nonlocal frames
            frames = iter(self._transport().frames(endpoint, transport_request))
            for frame in frames:
                evidence.observe(frame)
                yield frame

        try:
            decoded = self.response_hooks.normalize(
                endpoint_id=str(endpoint.endpoint_id), provider_id=str(endpoint.provider),
                model_id=str(endpoint.model_id), wire_shape=shape, request=request,
                updates=codec.decode(observed_frames(), context),
            )
            for update in decoded:
                response = replace(update.response, attempt_ids=(request_id,))
                last = replace(update, response=response)
                yield last
            if last is None or (not last.response.message.parts and last.response.finish_reason != LLMFinishReason.LENGTH):
                raise RuntimeError("LLM stream completed without semantic output")
            status = "failed" if last.response.finish_reason == LLMFinishReason.ERROR else "success"
        except BaseException as exc:
            status = "cancelled" if isinstance(exc, (GeneratorExit, asyncio.CancelledError, LLMStreamCancelledError)) or bool(
                stream_control and stream_control.cancelled
            ) else "failed"
            error_type = type(exc).__name__
            # Preserve the original exception type for existing retry policy.
            with contextlib.suppress(AttributeError, TypeError):
                exc.llm_attempt_recorded = self.attempt_sink is not None
            raise
        finally:
            for iterator in (decoded, frames):
                close = getattr(iterator, "close", None)
                if callable(close):
                    with contextlib.suppress(Exception):
                        close()
            usage = evidence.usage
            attempt = LLMAttemptResult(
                attempt_id=request_id, endpoint_id=str(endpoint.endpoint_id),
                model_id=str(endpoint.model_id), provider=str(endpoint.provider),
                status=status, usage=usage,
                provider_generation_id=evidence.provider_generation_id,
                returned_model=evidence.returned_model, actual_provider=evidence.actual_provider,
                service_tier=evidence.service_tier, elapsed_seconds=time.monotonic() - started_at,
                error_type=error_type,
            )
            diag = dict(
                diagnostics, request_id=request_id, endpoint_id=attempt.endpoint_id,
                model_id=attempt.model_id, provider_id=attempt.provider, wire_shape=shape.value,
                finish_reason=last.response.finish_reason.value if last else "",
                elapsed_seconds=attempt.elapsed_seconds, provider_generation_id=attempt.provider_generation_id,
                returned_model=attempt.returned_model, actual_provider=attempt.actual_provider,
                service_tier=attempt.service_tier,
            )
            if status == "success":
                self.prompt_cache.record_success(plan, usage,
                    applied_cache_breakpoint_message_ids=encoded.applied_cache_breakpoint_message_ids, **diag)
            else:
                self.prompt_cache.record_attempt(plan, status=status, usage=usage,
                    applied_cache_breakpoint_message_ids=encoded.applied_cache_breakpoint_message_ids,
                    error=error_type, **diag)
            if self.attempt_sink is not None:
                self.attempt_sink(attempt)
            report_attempt = getattr(self._transport(), "report_attempt", None)
            if callable(report_attempt):
                report_attempt(endpoint, attempt)
            else:
                report_usage = getattr(self._transport(), "report_usage", None)
                if callable(report_usage):
                    report_usage(endpoint, request_id=request_id, usage=usage, provider_response_count=1)

    def _transport(self) -> LLMJSONTransportPort:
        if self.transport is None:
            raise RuntimeError("LLM JSON transport is not configured")
        return self.transport

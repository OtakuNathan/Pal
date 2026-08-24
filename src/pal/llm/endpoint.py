from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pal.llm.ir import (
    LLMRequestIR,
    LLMResponseIR,
    LLMResponseUpdate,
    WireShape,
)
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
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
        *,
        stream: bool = False,
        timeout_seconds: float = 600.0,
    ) -> tuple[LLMResponseIR, tuple[LLMResponseUpdate, ...]]:
        shape = WireShape(str(endpoint.wire_shape))
        context = ShapeContext(
            wire_shape=shape,
            endpoint_id=str(endpoint.endpoint_id),
            model_id=str(endpoint.model_id),
            provider_id=str(endpoint.provider),
            base_url=str(endpoint.base_url or ""),
            capabilities=dict(endpoint.capabilities_blob or {}),
        )
        codec = codec_for_shape(shape)
        raw_encoded = codec.encode(request, context)
        plan = self.prompt_cache.plan(request, context, raw_encoded)
        encoded = self.prompt_cache.inject(raw_encoded, plan)
        request_id = f"llm_{uuid4().hex}"
        updates = tuple(
            self.response_hooks.normalize(
                endpoint_id=str(endpoint.endpoint_id),
                provider_id=str(endpoint.provider),
                model_id=str(endpoint.model_id),
                wire_shape=shape,
                request=request,
                updates=codec.decode(
                    self._transport().frames(
                        endpoint,
                        EncodedTransportRequest(
                            request_id=request_id,
                            wire_shape=shape,
                            timeout_seconds=float(timeout_seconds),
                            payload=encoded.payload,
                            extra_body=encoded.extra_body,
                            stream=bool(stream),
                        )
                    ),
                    context,
                ),
            )
        )
        if not updates:
            raise RuntimeError("LLM codec completed without a response")
        response = updates[-1].response
        if response.finish_reason != LLMFinishReason.ERROR:
            self.prompt_cache.record_success(
                plan,
                response.usage,
                applied_cache_breakpoint_message_ids=encoded.applied_cache_breakpoint_message_ids,
            )
        self._report_usage(endpoint, request_id, response)
        return response, updates

    def invoke_updates(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
        *,
        timeout_seconds: float = 600.0,
        stream_control: LLMStreamControl | None = None,
    ) -> Iterator[LLMResponseUpdate]:
        shape = WireShape(str(endpoint.wire_shape))
        context = ShapeContext(
            wire_shape=shape,
            endpoint_id=str(endpoint.endpoint_id),
            model_id=str(endpoint.model_id),
            provider_id=str(endpoint.provider),
            base_url=str(endpoint.base_url or ""),
            capabilities=dict(endpoint.capabilities_blob or {}),
        )
        codec = codec_for_shape(shape)
        raw_encoded = codec.encode(request, context)
        plan = self.prompt_cache.plan(request, context, raw_encoded)
        encoded = self.prompt_cache.inject(raw_encoded, plan)
        request_id = f"llm_{uuid4().hex}"
        last: LLMResponseUpdate | None = None
        decoded = codec.decode(
            self._transport().frames(
                endpoint,
                EncodedTransportRequest(
                    request_id=request_id,
                    wire_shape=shape,
                    timeout_seconds=float(timeout_seconds),
                    payload=encoded.payload,
                    extra_body=encoded.extra_body,
                    stream=True,
                    stream_control=stream_control,
                )
            ),
            context,
        )
        for update in self.response_hooks.normalize(
            endpoint_id=str(endpoint.endpoint_id),
            provider_id=str(endpoint.provider),
            model_id=str(endpoint.model_id),
            wire_shape=shape,
            request=request,
            updates=decoded,
        ):
            last = update
            yield update
        if last is None:
            raise RuntimeError("LLM stream completed without semantic output")
        if (
            not last.response.message.parts
            and last.response.finish_reason != LLMFinishReason.LENGTH
        ):
            raise RuntimeError("LLM stream completed without semantic output")
        if last.response.finish_reason != LLMFinishReason.ERROR:
            self.prompt_cache.record_success(
                plan,
                last.response.usage,
                applied_cache_breakpoint_message_ids=encoded.applied_cache_breakpoint_message_ids,
            )
        self._report_usage(endpoint, request_id, last.response)

    def _transport(self) -> LLMJSONTransportPort:
        if self.transport is None:
            raise RuntimeError("LLM JSON transport is not configured")
        return self.transport

    def _report_usage(
        self,
        endpoint: LLMEndpointModel,
        request_id: str,
        response: LLMResponseIR,
    ) -> None:
        report = getattr(self._transport(), "report_usage", None)
        if callable(report):
            report(
                endpoint,
                request_id=request_id,
                usage=response.usage,
                provider_response_count=response.provider_response_count,
            )

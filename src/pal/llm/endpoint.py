from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from pal.llm.ir import (
    LLMRequestIR,
    LLMResponseIR,
    LLMResponseUpdate,
    WireShape,
)
from pal.llm.credentials import LLMCredentialUnavailableError
from pal.llm.models import LLMEndpointModel
from pal.llm.response_hooks import ProviderResponseHookRegistry
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.llm.transport import SDKJSONTransport, SDKTransportRequest
from pal.shared import LLMFinishReason


CredentialResolver = Callable[[LLMEndpointModel], str | None]


@dataclass
class ShapeEndpointInvoker:
    credential_resolver: CredentialResolver
    transport: SDKJSONTransport = field(default_factory=SDKJSONTransport)
    response_hooks: ProviderResponseHookRegistry = field(
        default_factory=ProviderResponseHookRegistry.builtin
    )

    def refresh_credentials(self) -> bool:
        owner = getattr(self.credential_resolver, "__self__", None)
        refresh = getattr(owner, "refresh", None)
        if not callable(refresh):
            return False
        refresh()
        self.transport.close()
        return True

    def activate_endpoint(self, endpoint_id: str) -> None:
        self.transport.activate_endpoint(endpoint_id)

    def close(self) -> None:
        self.transport.close()

    def invoke(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
        *,
        stream: bool = False,
        timeout_seconds: float = 180.0,
    ) -> tuple[LLMResponseIR, tuple[LLMResponseUpdate, ...]]:
        shape = WireShape(str(endpoint.wire_shape))
        context = ShapeContext(
            wire_shape=shape,
            endpoint_id=str(endpoint.endpoint_id),
            model_id=str(endpoint.model_id),
        )
        codec = codec_for_shape(shape)
        encoded = codec.encode(request, context)
        updates = tuple(
            self.response_hooks.normalize(
                endpoint_id=str(endpoint.endpoint_id),
                provider_id=str(endpoint.provider),
                model_id=str(endpoint.model_id),
                wire_shape=shape,
                request=request,
                updates=codec.decode(
                    self.transport.frames(
                        SDKTransportRequest(
                            endpoint_id=str(endpoint.endpoint_id),
                            wire_shape=shape,
                            api_key=self._credential(endpoint),
                            base_url=str(endpoint.base_url or ""),
                            timeout_seconds=float(timeout_seconds),
                            payload=encoded.payload,
                            stream=bool(stream),
                        )
                    ),
                    context,
                ),
            )
        )
        if not updates:
            raise RuntimeError("LLM codec completed without a response")
        return updates[-1].response, updates

    def invoke_updates(
        self,
        endpoint: LLMEndpointModel,
        request: LLMRequestIR,
        *,
        timeout_seconds: float = 180.0,
    ) -> Iterator[LLMResponseUpdate]:
        shape = WireShape(str(endpoint.wire_shape))
        context = ShapeContext(
            wire_shape=shape,
            endpoint_id=str(endpoint.endpoint_id),
            model_id=str(endpoint.model_id),
        )
        codec = codec_for_shape(shape)
        encoded = codec.encode(request, context)
        last: LLMResponseUpdate | None = None
        decoded = codec.decode(
            self.transport.frames(
                SDKTransportRequest(
                    endpoint_id=str(endpoint.endpoint_id),
                    wire_shape=shape,
                    api_key=self._credential(endpoint),
                    base_url=str(endpoint.base_url or ""),
                    timeout_seconds=float(timeout_seconds),
                    payload=encoded.payload,
                    stream=True,
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

    def _credential(self, endpoint: LLMEndpointModel) -> str:
        value = str(self.credential_resolver(endpoint) or "")
        if endpoint.auth_kind != "local_provider_auth" and not value:
            raise LLMCredentialUnavailableError(
                f"LLM endpoint {endpoint.endpoint_id} has no usable credential"
            )
        return value

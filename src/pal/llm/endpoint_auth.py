"""Endpoint-bound credential access and access-profile failure policy."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import MethodType

from pal.llm.chatgpt import ChatGPTAuthService, ChatGPTError, QUOTA_CODE, exception_error, response_error
from pal.llm.credentials import LLMCredentialResolver, LLMCredentialUnavailableError
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.shapes.base import _JSONFrame
from pal.llm.transport_contracts import LLMHTTPTransportError, LLMProviderStartedError, LLMTransportError


@dataclass(frozen=True)
class EndpointAuth:
    endpoint: LLMEndpointSpec
    resolver: Callable[[LLMEndpointSpec], str | None]
    force_stream: bool = field(default=False, init=False)

    def resolve(self) -> str:
        credential = str(self.resolver(self.endpoint) or "")
        if self.endpoint.auth_kind != "local_provider_auth" and not credential:
            raise LLMCredentialUnavailableError(f"LLM endpoint {self.endpoint.endpoint_id} has no usable credential")
        return credential

    def observe(self, frame: _JSONFrame) -> None:
        pass

    def failure(self, exc: Exception) -> Exception:
        return exc

    def resume(self) -> None:
        pass


@dataclass(frozen=True)
class ChatGPTEndpointAuth(EndpointAuth):
    force_stream: bool = field(default=True, init=False)

    def observe(self, frame: _JSONFrame) -> None:
        if frame.payload.get("type") in {"response.failed", "error"}:
            failure = response_error(frame.payload.get("response") or frame.payload)
            if failure.code == QUOTA_CODE:
                self._usage_state(paused=True)

    def failure(self, exc: Exception) -> Exception:
        failure = exception_error(exc, include_http=True)
        if failure is None:
            if isinstance(exc, LLMHTTPTransportError):
                failure = response_error(exc.payload, status=exc.status, request_id=exc.request_id)
            elif isinstance(exc, LLMProviderStartedError):
                failure = ChatGPTError("request_outcome_unknown", diagnostic=str(exc))
            elif isinstance(exc, LLMTransportError):
                failure = ChatGPTError("network_error")
        if failure is None:
            return exc
        if failure.code == QUOTA_CODE:
            self._usage_state(paused=True)
        return failure

    def _usage_state(self, *, paused: bool) -> None:
        owner = self.resolver.__self__ if isinstance(self.resolver, MethodType) else None
        if isinstance(owner, LLMCredentialResolver) and owner.secret_store is not None:
            ref = owner.secret_ref_for_endpoint(self.endpoint)
            if ref is not None:
                service = ChatGPTAuthService(owner.secret_store)
                (service.pause if paused else service.resume)(ref)

    def resume(self) -> None:
        self._usage_state(paused=False)

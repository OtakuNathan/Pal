"""Semantic invocation and optional endpoint lifecycle contracts.

Every invoker returns accepted IR. Raw protocol adaptation belongs below this
boundary, before semantic decoding; Runtime never reinterprets its output.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any, Protocol, runtime_checkable

from pal.llm.attempts import LLMAttemptResult
from pal.llm.ir import LLMRequestIR, LLMResponseIR, LLMResponseUpdate
from pal.llm.models import LLMEndpointModel
from pal.llm.request_contracts import RequestSubmission
from pal.llm.response_hooks import ProviderResponseHookRegistry
from pal.llm.shapes.base import EncodedRequest
from pal.llm.transport_contracts import LLMStreamControl


class LLMEndpointInvokerPort(Protocol):
    def invoke(
        self, endpoint: LLMEndpointModel, request: LLMRequestIR, *,
        stream: bool = False, timeout_seconds: float = 600.0,
    ) -> tuple[LLMResponseIR, tuple[LLMResponseUpdate, ...]]: ...

    def invoke_updates(
        self, endpoint: LLMEndpointModel, request: LLMRequestIR, *,
        timeout_seconds: float = 600.0, stream_control: LLMStreamControl | None = None,
    ) -> Iterator[LLMResponseUpdate]: ...


@runtime_checkable
class ManagedEndpointInvokerPort(LLMEndpointInvokerPort, Protocol):
    """Endpoint-bound invocation with lifecycle, receipts and attempt accounting."""

    @property
    def response_hooks(self) -> ProviderResponseHookRegistry: ...

    def configure_response_hooks(self, registry: ProviderResponseHookRegistry) -> None: ...

    def configure_attempt_sink(self, sink: Callable[[LLMAttemptResult], Any]) -> None: ...

    def prepare_endpoints(self, endpoints: Iterable[LLMEndpointModel]) -> None: ...

    def fork_transient(self) -> ManagedEndpointInvokerPort: ...

    def resume_subscription(self, endpoint: LLMEndpointModel) -> None: ...

    def invoke(
        self, endpoint: LLMEndpointModel, request: LLMRequestIR, *,
        stream: bool = False, timeout_seconds: float = 600.0,
        projection: EncodedRequest | None = None,
        submission_sink: Callable[[RequestSubmission], None] | None = None,
    ) -> tuple[LLMResponseIR, tuple[LLMResponseUpdate, ...]]: ...

    def invoke_updates(
        self, endpoint: LLMEndpointModel, request: LLMRequestIR, *,
        timeout_seconds: float = 600.0, stream_control: LLMStreamControl | None = None,
        projection: EncodedRequest | None = None,
        submission_sink: Callable[[RequestSubmission], None] | None = None,
    ) -> Iterator[LLMResponseUpdate]: ...

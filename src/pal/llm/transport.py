from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol

import httpx

from pal.foundation.fd_lease import (
    FdCancellationControl,
    FdCapability,
    FdCloseOutcome,
    FdLease,
    FdLeaseCancelledError,
    FdLeaseInvariantError,
)
from pal.llm.ir import WireShape
from pal.llm.models import LLMEndpointModel
from pal.llm.shapes.base import _JSONFrame
from pal.shared.json_values import thaw_json


class LLMTransportError(RuntimeError):
    pass


LLMStreamCancelledError = FdLeaseCancelledError


@dataclass
class LLMStreamControl:
    """LLM telemetry plus an fd capability cancellation/revocation gate."""

    started_at: float = field(default_factory=time.monotonic)
    _fd: FdCancellationControl = field(default_factory=FdCancellationControl, repr=False)
    _provider_started: bool = field(default=False, init=False, repr=False)
    _last_network_activity_at: float = field(default=0.0, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @property
    def cancelled(self) -> bool:
        return self._fd.cancelled

    @property
    def cancel_reason(self) -> str:
        return self._fd.cancel_reason

    @property
    def provider_started(self) -> bool:
        with self._lock:
            return self._provider_started

    @property
    def last_network_activity_at(self) -> float:
        with self._lock:
            return max(self.started_at, self._last_network_activity_at)

    def bind(self, capability: FdCapability[Any]) -> None:
        self._fd.bind(capability)

    def unbind(self, capability: FdCapability[Any]) -> None:
        self._fd.unbind(capability)

    def cancel(self, reason: str = "cancelled") -> None:
        self._fd.cancel(reason)

    def raise_if_cancelled(self) -> None:
        self._fd.raise_if_cancelled()

    def mark_provider_started(self) -> None:
        with self._lock:
            self._provider_started = True
            self._last_network_activity_at = time.monotonic()

    def touch_network(self) -> None:
        self.mark_provider_started()


class LLMEndpointSpecStaleError(LLMTransportError):
    """The proxy rejected an endpoint snapshot before starting the provider."""


class LLMProviderStartedError(LLMTransportError):
    """A remote request failed after provider execution had already started."""


@dataclass(frozen=True)
class EncodedTransportRequest:
    """Provider-shaped request passed beneath the shared LLM pipeline."""

    request_id: str
    wire_shape: WireShape
    timeout_seconds: float
    payload: Mapping[str, Any]
    stream: bool
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    stream_control: "LLMStreamControl | None" = None


class LLMJSONTransportPort(Protocol):
    """Transport-only boundary shared by resident and remote Bunshin runtimes."""

    def frames(
        self,
        endpoint: LLMEndpointModel,
        request: EncodedTransportRequest,
    ) -> Iterator[_JSONFrame]:
        ...


class _ObservedSyncByteStream(httpx.SyncByteStream):
    def __init__(self, inner: httpx.SyncByteStream, on_chunk: Callable[[], None]) -> None:
        self._inner = inner
        self._on_chunk = on_chunk

    def __iter__(self):
        for chunk in self._inner:
            self._on_chunk()
            yield chunk

    def close(self) -> None:
        self._inner.close()


class SDKClientFactory(Protocol):
    def openai(self, *, api_key: str, base_url: str, timeout: float) -> Any:
        ...

    def anthropic(self, *, api_key: str, base_url: str, timeout: float) -> Any:
        ...


@dataclass(frozen=True)
class DefaultSDKClientFactory:
    def openai(self, *, api_key: str, base_url: str, timeout: float) -> Any:
        from openai import OpenAI

        # Pal owns retry/fallback decisions. Nested SDK retries can repeat an
        # expensive generation before Core has observed any outcome.
        return OpenAI(
            api_key=api_key,
            base_url=base_url or None,
            timeout=timeout,
            max_retries=0,
        )

    def anthropic(self, *, api_key: str, base_url: str, timeout: float) -> Any:
        from anthropic import Anthropic

        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout,
            "max_retries": 0,
        }
        if base_url:
            kwargs["base_url"] = base_url
        return Anthropic(**kwargs)


@dataclass(frozen=True)
class SDKTransportRequest:
    request_id: str
    endpoint_id: str
    wire_shape: WireShape
    api_key: str
    base_url: str
    timeout_seconds: float
    payload: Mapping[str, Any]
    stream: bool
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    stream_control: LLMStreamControl | None = None


@dataclass
class DirectSDKTransport:
    """Resolve local credentials and execute an encoded request through an SDK."""

    credential_resolver: Callable[[LLMEndpointModel], str | None]
    sdk_transport: Any = field(default_factory=lambda: SDKJSONTransport())

    def frames(
        self,
        endpoint: LLMEndpointModel,
        request: EncodedTransportRequest,
    ) -> Iterator[_JSONFrame]:
        api_key = str(self.credential_resolver(endpoint) or "")
        if endpoint.auth_kind != "local_provider_auth" and not api_key:
            from pal.llm.credentials import LLMCredentialUnavailableError

            raise LLMCredentialUnavailableError(
                f"LLM endpoint {endpoint.endpoint_id} has no usable credential"
            )
        yield from self.sdk_transport.frames(
            SDKTransportRequest(
                request_id=request.request_id,
                endpoint_id=str(endpoint.endpoint_id),
                wire_shape=request.wire_shape,
                api_key=api_key,
                base_url=str(endpoint.base_url or ""),
                timeout_seconds=float(request.timeout_seconds),
                payload=request.payload,
                extra_body=request.extra_body,
                stream=bool(request.stream),
                stream_control=request.stream_control,
            )
        )

    def refresh_credentials(self) -> bool:
        owner = getattr(self.credential_resolver, "__self__", None)
        refresh = getattr(owner, "refresh", None)
        if not callable(refresh):
            return False
        refresh()
        self.close()
        return True

    def activate_endpoint(self, endpoint_id: str) -> None:
        activate = getattr(self.sdk_transport, "activate_endpoint", None)
        if callable(activate):
            activate(endpoint_id)

    def close(self) -> None:
        close = getattr(self.sdk_transport, "close", None)
        if callable(close):
            close()


@dataclass
class SDKJSONTransport:
    client_factory: SDKClientFactory = DefaultSDKClientFactory()
    _clients: dict[tuple[str, ...], list["_ClientEntry"]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _active_endpoint_id: str = field(default="", init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def frames(self, request: SDKTransportRequest) -> Iterator[_JSONFrame]:
        payload = thaw_json(request.payload)
        extra_body = thaw_json(request.extra_body)
        if extra_body:
            payload["extra_body"] = extra_body
        payload["stream"] = bool(request.stream)
        if request.stream and request.wire_shape == WireShape.OPENAI_COMPLETION:
            payload.setdefault("stream_options", {"include_usage": True})
        key, entry, capability = self._acquire(request)
        control = request.stream_control
        succeeded = False
        if control is not None:
            control.bind(capability)
        try:
            if control is not None:
                control.raise_if_cancelled()
            capability.call_sync(
                lambda graph: _open_sdk_response(graph, request, payload)
            )
            if control is not None:
                control.mark_provider_started()
                capability.call_sync(
                    lambda graph: _observe_sdk_graph_stream(
                        graph,
                        control.touch_network,
                    )
                )
                control.raise_if_cancelled()
            while True:
                if control is not None:
                    control.raise_if_cancelled()
                try:
                    frame = capability.call_sync(_next_sdk_frame)
                except StopIteration:
                    break
                yield frame
            if control is not None:
                control.raise_if_cancelled()
            succeeded = capability.call_sync(_close_active_sdk_response)
        except Exception as exc:
            if isinstance(exc, (LLMTransportError, FdLeaseCancelledError)):
                raise
            raise LLMTransportError(f"{request.wire_shape.value} request failed: {exc}") from exc
        finally:
            capability.release_sync(reuse=succeeded)
            if control is not None:
                control.unbind(capability)
            self._settle(key, entry)

    def activate_endpoint(self, endpoint_id: str) -> None:
        normalized = str(endpoint_id or "").strip()
        closing: list[_ClientEntry] = []
        with self._lock:
            if normalized == self._active_endpoint_id:
                return
            self._active_endpoint_id = normalized
            for key, entries in self._clients.items():
                if key[0] == normalized:
                    continue
                for entry in entries:
                    if entry.owner.request_retire("endpoint_deactivated"):
                        closing.append(entry)
        for entry in closing:
            self._close_idle_entry(entry)
        self._discard_closed_entries()

    def close(self) -> None:
        closing: list[_ClientEntry] = []
        with self._lock:
            for entries in self._clients.values():
                for entry in entries:
                    if entry.owner.request_retire("transport_close"):
                        closing.append(entry)
            self._active_endpoint_id = ""
        for entry in closing:
            self._close_idle_entry(entry)
        self._discard_closed_entries()

    def _acquire(
        self,
        request: SDKTransportRequest,
    ) -> tuple[tuple[str, ...], "_ClientEntry", FdCapability["_SDKClientGraph"]]:
        key = _client_key(request)
        with self._lock:
            entries = self._clients.setdefault(key, [])
            entry = next(
                (
                    candidate
                    for candidate in entries
                    if candidate.owner.reusable
                ),
                None,
            )
            if entry is None:
                client = (
                    self.client_factory.anthropic(
                        api_key=request.api_key,
                        base_url=request.base_url,
                        timeout=request.timeout_seconds,
                    )
                    if request.wire_shape == WireShape.ANTHROPIC_MESSAGES
                    else self.client_factory.openai(
                        api_key=request.api_key,
                        base_url=request.base_url,
                        timeout=request.timeout_seconds,
                    )
                )
                entry = _ClientEntry(
                    owner=FdLease(
                        resource_kind=f"llm.sdk_client:{request.endpoint_id}",
                        _resource=_SDKClientGraph(client=client),
                        capacity=1,
                        closer_sync=_close_sdk_graph,
                        hard_closer_sync=_close_sdk_graph,
                    ),
                )
                entries.append(entry)
            lease = entry.owner.acquire(operation_id=request.request_id)
            return key, entry, lease

    def _settle(
        self,
        key: tuple[str, ...],
        entry: "_ClientEntry",
    ) -> None:
        with self._lock:
            if not (entry.owner.closed or entry.owner.quarantined):
                return
            entries = self._clients.get(key, [])
            if entry in entries:
                entries.remove(entry)
            if not entries:
                self._clients.pop(key, None)

    def _close_idle_entry(self, entry: "_ClientEntry") -> None:
        entry.owner.close_sync()

    def _discard_closed_entries(self) -> None:
        with self._lock:
            for key, entries in tuple(self._clients.items()):
                remaining = [
                    entry
                    for entry in entries
                    if not (entry.owner.closed or entry.owner.quarantined)
                ]
                if remaining:
                    self._clients[key] = remaining
                else:
                    self._clients.pop(key, None)

@dataclass
class _ClientEntry:
    owner: FdLease["_SDKClientGraph"]


@dataclass
class _SDKClientGraph:
    client: Any
    response: Any = None
    frames: Iterator[_JSONFrame] | None = None
    close_errors: list[str] = field(default_factory=list)
    response_close_attempted: bool = False


def _client_key(request: SDKTransportRequest) -> tuple[str, ...]:
    credential_fingerprint = hashlib.sha256(
        request.api_key.encode("utf-8")
    ).hexdigest()
    return (
        str(request.endpoint_id),
        request.wire_shape.value,
        str(request.base_url),
        f"{float(request.timeout_seconds):.9g}",
        credential_fingerprint,
    )


def _open_sdk_response(
    graph: _SDKClientGraph,
    request: SDKTransportRequest,
    payload: dict[str, Any],
) -> None:
    if graph.response is not None or graph.frames is not None:
        raise FdLeaseInvariantError("SDK client graph already has an active response")
    if request.wire_shape == WireShape.ANTHROPIC_MESSAGES:
        operation = graph.client.messages.create
    elif request.wire_shape == WireShape.OPENAI_RESPONSE:
        operation = graph.client.responses.create
    else:
        operation = graph.client.chat.completions.create
    response = operation(**payload)
    graph.response = response
    graph.frames = iter(_iter_json_frames(response))
    graph.response_close_attempted = False


def _observe_sdk_graph_stream(
    graph: _SDKClientGraph,
    on_chunk: Callable[[], None],
) -> None:
    if graph.response is None:
        raise FdLeaseInvariantError("SDK client graph has no active response")
    _observe_raw_response_stream(graph.response, on_chunk)


def _next_sdk_frame(graph: _SDKClientGraph) -> _JSONFrame:
    frames = graph.frames
    if frames is None:
        raise FdLeaseInvariantError("SDK client graph has no active frame iterator")
    return next(frames)


def _close_active_sdk_response(graph: _SDKClientGraph) -> bool:
    response = graph.response
    if response is None:
        graph.frames = None
        return True
    if graph.response_close_attempted:
        return False
    graph.response_close_attempted = True
    try:
        _close_stream_response_or_raise(response)
    except BaseException as exc:
        graph.close_errors.append(f"response:{type(exc).__name__}:{exc}")
        # An uncertain close keeps the response and iterator strongly bound to
        # the quarantined graph. Never manufacture detachment by clearing them.
        return False
    graph.response = None
    graph.frames = None
    graph.response_close_attempted = False
    return True


def _close_sdk_graph(graph: _SDKClientGraph) -> FdCloseOutcome:
    errors = list(graph.close_errors)
    response = graph.response
    if response is not None and not graph.response_close_attempted:
        graph.response_close_attempted = True
        try:
            _close_stream_response_or_raise(response)
        except BaseException as exc:
            errors.append(f"response:{type(exc).__name__}:{exc}")
        else:
            graph.response = None
            graph.frames = None
            graph.response_close_attempted = False
    try:
        _close_client_or_raise(graph.client)
    except BaseException as exc:
        errors.append(f"client:{type(exc).__name__}:{exc}")
    if errors:
        graph.close_errors = errors
        return FdCloseOutcome.uncertain("; ".join(errors))
    graph.close_errors.clear()
    return FdCloseOutcome.detached()


def _close_client_or_raise(client: Any) -> None:
    close = getattr(client, "close", None)
    if not callable(close):
        return
    close()


def _close_stream_response_or_raise(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()
        return
    raw = _raw_http_response(response)
    close = getattr(raw, "close", None)
    if not callable(close):
        return
    close()


def _raw_http_response(response: Any) -> Any:
    for attribute in ("response", "_response"):
        raw = getattr(response, attribute, None)
        if raw is not None:
            return raw
    return None


def _observe_raw_response_stream(response: Any, on_chunk: Callable[[], None]) -> None:
    raw = _raw_http_response(response)
    stream = getattr(raw, "stream", None)
    if not isinstance(stream, httpx.SyncByteStream):
        return
    if isinstance(stream, _ObservedSyncByteStream):
        return
    raw.stream = _ObservedSyncByteStream(stream, on_chunk)


def _iter_json_frames(response: Any) -> Iterator[_JSONFrame]:
    """Normalize SDK single-shot objects and streaming iterables into JSON frames."""

    if _is_single_json_value(response):
        yield _JSONFrame(0, _json_mapping(response))
        return
    try:
        iterator = iter(response)
    except TypeError as exc:
        raise LLMTransportError(
            f"SDK response is neither a JSON object nor an iterable: {type(response).__name__}"
        ) from exc
    for sequence, item in enumerate(iterator):
        yield _JSONFrame(sequence, _json_mapping(item))


def _is_single_json_value(value: Any) -> bool:
    if isinstance(value, Mapping):
        return True
    return callable(getattr(value, "model_dump", None)) or callable(getattr(value, "to_dict", None))


def _json_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="json", exclude_none=True)
        if isinstance(payload, Mapping):
            return dict(payload)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, Mapping):
            return dict(payload)
    raise LLMTransportError(f"SDK frame is not JSON-object shaped: {type(value).__name__}")

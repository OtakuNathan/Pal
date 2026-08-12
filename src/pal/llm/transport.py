from __future__ import annotations

import hashlib
import time
from contextlib import suppress
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol

import httpx

from pal.llm.ir import WireShape
from pal.llm.models import LLMEndpointModel
from pal.llm.shapes.base import _JSONFrame
from pal.shared.json_values import thaw_json


class LLMTransportError(RuntimeError):
    pass


class LLMStreamCancelledError(LLMTransportError):
    pass


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
    stream_control: "StreamControl | None" = None


class LLMJSONTransportPort(Protocol):
    """Transport-only boundary shared by resident and remote Minion runtimes."""

    def frames(
        self,
        endpoint: LLMEndpointModel,
        request: EncodedTransportRequest,
    ) -> Iterator[_JSONFrame]:
        ...


@dataclass
class StreamControl:
    """Per-invocation cancellation and raw-network liveness ownership."""

    started_at: float = field(default_factory=time.monotonic)
    _last_network_activity_at: float = field(init=False, repr=False)
    _provider_started: bool = field(default=False, init=False, repr=False)
    _cancelled: bool = field(default=False, init=False, repr=False)
    _cancel_reason: str = field(default="", init=False, repr=False)
    _client: Any = field(default=None, init=False, repr=False)
    _response: Any = field(default=None, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        self._last_network_activity_at = self.started_at

    @property
    def provider_started(self) -> bool:
        with self._lock:
            return self._provider_started

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    @property
    def cancel_reason(self) -> str:
        with self._lock:
            return self._cancel_reason

    @property
    def last_network_activity_at(self) -> float:
        with self._lock:
            return self._last_network_activity_at

    def bind_client(self, client: Any) -> None:
        close_now = False
        with self._lock:
            self._client = client
            close_now = self._cancelled
        if close_now:
            _close_client(client)

    def bind_response(self, response: Any) -> None:
        close_now = False
        with self._lock:
            self._response = response
            self._provider_started = True
            self._last_network_activity_at = time.monotonic()
            close_now = self._cancelled
        _observe_raw_response_stream(response, self.touch_network)
        if close_now:
            _close_stream_response(response)

    def touch_network(self) -> None:
        with self._lock:
            self._provider_started = True
            self._last_network_activity_at = time.monotonic()

    def release(self, *, client: Any, response: Any = None) -> None:
        with self._lock:
            if self._client is client:
                self._client = None
            if response is not None and self._response is response:
                self._response = None

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if self._cancelled:
                return
            self._cancelled = True
            self._cancel_reason = str(reason or "cancelled")
            response = self._response
            client = self._client
        if response is not None:
            _close_stream_response(response)
        if client is not None:
            _close_client(client)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise LLMStreamCancelledError(
                f"LLM stream cancelled: {self.cancel_reason or 'cancelled'}"
            )


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
    endpoint_id: str
    wire_shape: WireShape
    api_key: str
    base_url: str
    timeout_seconds: float
    payload: Mapping[str, Any]
    stream: bool
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    stream_control: StreamControl | None = None


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
        key, entry = self._acquire(request)
        response: Any = None
        control = request.stream_control
        if control is not None:
            control.bind_client(entry.client)
        try:
            if control is not None:
                control.raise_if_cancelled()
            operation = self._operation(entry.client, request)
            response = operation(**payload)
            if control is not None:
                control.bind_response(response)
                control.raise_if_cancelled()
            yield from _iter_json_frames(response)
            if control is not None:
                control.raise_if_cancelled()
        except Exception as exc:
            if isinstance(exc, LLMTransportError):
                raise
            raise LLMTransportError(f"{request.wire_shape.value} request failed: {exc}") from exc
        finally:
            retire_entry = False
            if control is not None:
                control.release(client=entry.client, response=response)
                retire_entry = control.cancelled
            if response is not None and request.stream:
                _close_stream_response(response)
            self._release(key, entry, retire=retire_entry)

    def activate_endpoint(self, endpoint_id: str) -> None:
        normalized = str(endpoint_id or "").strip()
        retired: list[Any] = []
        with self._lock:
            if normalized == self._active_endpoint_id:
                return
            self._active_endpoint_id = normalized
            for key, entries in tuple(self._clients.items()):
                if key[0] == normalized:
                    continue
                for entry in entries:
                    entry.retired = True
                    if entry.leases == 0:
                        retired.append(entry.client)
                remaining = [entry for entry in entries if entry.leases > 0]
                if remaining:
                    self._clients[key] = remaining
                else:
                    self._clients.pop(key, None)
        for client in retired:
            _close_client(client)

    def close(self) -> None:
        clients: list[Any] = []
        with self._lock:
            for key, entries in tuple(self._clients.items()):
                for entry in entries:
                    entry.retired = True
                    if entry.leases == 0:
                        clients.append(entry.client)
                remaining = [entry for entry in entries if entry.leases > 0]
                if remaining:
                    self._clients[key] = remaining
                else:
                    self._clients.pop(key, None)
            self._active_endpoint_id = ""
        for client in clients:
            _close_client(client)

    def _acquire(
        self,
        request: SDKTransportRequest,
    ) -> tuple[tuple[str, ...], "_ClientEntry"]:
        key = _client_key(request)
        with self._lock:
            entries = self._clients.setdefault(key, [])
            entry = next(
                (
                    candidate
                    for candidate in entries
                    if not candidate.retired and candidate.leases == 0
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
                entry = _ClientEntry(client=client)
                entries.append(entry)
            entry.leases += 1
            return key, entry

    def _release(
        self,
        key: tuple[str, ...],
        leased_entry: "_ClientEntry",
        *,
        retire: bool = False,
    ) -> None:
        client = None
        with self._lock:
            if retire:
                leased_entry.retired = True
            leased_entry.leases = max(0, leased_entry.leases - 1)
            if leased_entry.retired and leased_entry.leases == 0:
                entries = self._clients.get(key, [])
                if leased_entry in entries:
                    entries.remove(leased_entry)
                    if not entries:
                        self._clients.pop(key, None)
                client = leased_entry.client
        if client is not None:
            _close_client(client)

    @staticmethod
    def _operation(client: Any, request: SDKTransportRequest) -> Callable[..., Any]:
        if request.wire_shape == WireShape.ANTHROPIC_MESSAGES:
            return client.messages.create
        if request.wire_shape == WireShape.OPENAI_RESPONSE:
            return client.responses.create
        return client.chat.completions.create


@dataclass
class _ClientEntry:
    client: Any
    leases: int = 0
    retired: bool = False


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


def _close_client(client: Any) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        with suppress(Exception):
            close()


def _close_stream_response(response: Any) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        with suppress(Exception):
            close()
        return
    raw = _raw_http_response(response)
    close = getattr(raw, "close", None)
    if callable(close):
        with suppress(Exception):
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

from __future__ import annotations

import hashlib
from contextlib import suppress
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol

from pal.llm.ir import WireShape
from pal.llm.shapes.base import _JSONFrame
from pal.shared.json_values import thaw_json


class LLMTransportError(RuntimeError):
    pass


class SDKClientFactory(Protocol):
    def openai(self, *, api_key: str, base_url: str, timeout: float) -> Any:
        ...

    def anthropic(self, *, api_key: str, base_url: str, timeout: float) -> Any:
        ...


@dataclass(frozen=True)
class DefaultSDKClientFactory:
    def openai(self, *, api_key: str, base_url: str, timeout: float) -> Any:
        from openai import OpenAI

        return OpenAI(api_key=api_key, base_url=base_url or None, timeout=timeout)

    def anthropic(self, *, api_key: str, base_url: str, timeout: float) -> Any:
        from anthropic import Anthropic

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
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


@dataclass
class SDKJSONTransport:
    client_factory: SDKClientFactory = DefaultSDKClientFactory()
    _clients: dict[tuple[str, ...], "_ClientEntry"] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _active_endpoint_id: str = field(default="", init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def frames(self, request: SDKTransportRequest) -> Iterator[_JSONFrame]:
        payload = thaw_json(request.payload)
        payload["stream"] = bool(request.stream)
        if request.stream and request.wire_shape == WireShape.OPENAI_COMPLETION:
            payload.setdefault("stream_options", {"include_usage": True})
        key, entry = self._acquire(request)
        try:
            operation = self._operation(entry.client, request)
            response = operation(**payload)
            yield from _iter_json_frames(response)
        except Exception as exc:
            if isinstance(exc, LLMTransportError):
                raise
            raise LLMTransportError(f"{request.wire_shape.value} request failed: {exc}") from exc
        finally:
            self._release(key, entry)

    def activate_endpoint(self, endpoint_id: str) -> None:
        normalized = str(endpoint_id or "").strip()
        retired: list[Any] = []
        with self._lock:
            if normalized == self._active_endpoint_id:
                return
            self._active_endpoint_id = normalized
            for key, entry in tuple(self._clients.items()):
                if key[0] == normalized:
                    continue
                entry.retired = True
                if entry.leases == 0:
                    self._clients.pop(key, None)
                    retired.append(entry.client)
        for client in retired:
            _close_client(client)

    def close(self) -> None:
        clients: list[Any] = []
        with self._lock:
            for key, entry in tuple(self._clients.items()):
                entry.retired = True
                if entry.leases == 0:
                    self._clients.pop(key, None)
                    clients.append(entry.client)
            self._active_endpoint_id = ""
        for client in clients:
            _close_client(client)

    def _acquire(
        self,
        request: SDKTransportRequest,
    ) -> tuple[tuple[str, ...], "_ClientEntry"]:
        key = _client_key(request)
        with self._lock:
            entry = self._clients.get(key)
            if entry is None or entry.retired:
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
                self._clients[key] = entry
            entry.leases += 1
            return key, entry

    def _release(self, key: tuple[str, ...], leased_entry: "_ClientEntry") -> None:
        client = None
        with self._lock:
            leased_entry.leases = max(0, leased_entry.leases - 1)
            if leased_entry.retired and leased_entry.leases == 0:
                if self._clients.get(key) is leased_entry:
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

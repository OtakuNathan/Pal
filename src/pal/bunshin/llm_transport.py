from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from pal.foundation.sidecar import SidecarRpcError
from pal.llm.endpoint_spec import endpoint_spec_fingerprint
from pal.llm.ir import LLMUsageIR
from pal.llm.models import LLMEndpointModel
from pal.llm.shapes.base import _JSONFrame
from pal.llm.transport import (
    EncodedTransportRequest,
    LLMEndpointSpecStaleError,
    LLMProviderStartedError,
    LLMTransportError,
)
from pal.bunshin.ipc import (
    ROLE_GATEWAY_TOKEN_ENV,
    BunshinManagerClient,
    BunshinRoleGatewayClient,
)
from pal.shared.json_values import thaw_json


@dataclass
class ManagerProxyTransport:
    """Blocking raw-frame transport used by the shared Bunshin LLM runtime."""

    runtime_root: Path
    run_id: str
    request_timeout_seconds: float = 3900.0
    receipt_timeout_seconds: float = 2.0
    last_receipt_error: str = field(default="", init=False)
    _active_streams: set[Any] = field(default_factory=set, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @property
    def _client(self) -> BunshinManagerClient | BunshinRoleGatewayClient:
        return self._client_with_timeout(self.request_timeout_seconds)

    def _client_with_timeout(
        self,
        timeout_seconds: float,
    ) -> BunshinManagerClient | BunshinRoleGatewayClient:
        access_token = str(os.environ.get(ROLE_GATEWAY_TOKEN_ENV) or "").strip()
        if access_token:
            return BunshinRoleGatewayClient(
                runtime_root=Path(self.runtime_root),
                access_token=access_token,
                request_timeout_seconds=float(timeout_seconds),
            )
        if os.environ.get("PAL_BUNSHIN_SANDBOXED") == "1":
            raise RuntimeError(
                "sandboxed bunshin has no assignment-scoped role gateway token"
            )
        return BunshinManagerClient(
            runtime_root=Path(self.runtime_root),
            request_timeout_seconds=float(timeout_seconds),
        )

    def frames(
        self,
        endpoint: LLMEndpointModel,
        request: EncodedTransportRequest,
    ) -> Iterator[_JSONFrame]:
        control = request.stream_control
        if control is not None:
            control.raise_if_cancelled()
        params = {
            "run_id": self.run_id,
            "request_id": request.request_id,
            "endpoint_id": str(endpoint.endpoint_id),
            "endpoint_spec_fingerprint": endpoint_spec_fingerprint(endpoint),
            "wire_shape": request.wire_shape.value,
            "timeout_seconds": float(request.timeout_seconds),
            "stream": bool(request.stream),
            "payload": thaw_json(request.payload),
            "extra_body": thaw_json(request.extra_body),
        }
        stream = self._client.stream_sync("llm_transport_stream", params)
        with self._lock:
            self._active_streams.add(stream)
        if control is not None:
            control.bind_client(stream)
            control.raise_if_cancelled()
        expected_sequence = 0
        provider_started = False
        try:
            for item in stream:
                event = str(item.get("event") or "")
                if event == "provider_started":
                    provider_started = True
                    if control is not None:
                        control.touch_network()
                    continue
                frame_payload = item.get("frame")
                if not isinstance(frame_payload, Mapping):
                    raise LLMTransportError("manager proxy emitted a malformed raw frame")
                sequence = int(frame_payload.get("sequence", -1))
                payload = frame_payload.get("payload")
                if sequence != expected_sequence:
                    raise LLMTransportError(
                        "manager proxy raw frame sequence is not contiguous"
                    )
                if not isinstance(payload, Mapping):
                    raise LLMTransportError("manager proxy raw frame payload is not an object")
                if control is not None:
                    control.touch_network()
                    control.raise_if_cancelled()
                yield _JSONFrame(sequence, dict(payload))
                expected_sequence += 1
        except SidecarRpcError as exc:
            if control is not None and control.cancelled:
                control.raise_if_cancelled()
            provider_started = bool(
                provider_started
                or dict(exc.payload or {}).get("provider_started")
                or (control is not None and control.provider_started)
            )
            if provider_started and control is not None:
                control.touch_network()
            if exc.kind == "endpoint_spec_stale" and not provider_started:
                raise LLMEndpointSpecStaleError(str(exc)) from exc
            if provider_started:
                raise LLMProviderStartedError(str(exc)) from exc
            raise LLMTransportError(str(exc)) from exc
        finally:
            if control is not None:
                control.release(client=stream)
            with self._lock:
                self._active_streams.discard(stream)
            stream.close()

    def report_usage(
        self,
        endpoint: LLMEndpointModel,
        *,
        request_id: str,
        usage: LLMUsageIR,
        provider_response_count: int,
    ) -> None:
        payload = {
            "run_id": self.run_id,
            "request_id": str(request_id),
            "endpoint_id": str(endpoint.endpoint_id),
            "model_id": str(endpoint.model_id),
            "provider": str(endpoint.provider),
            "provider_response_count": int(provider_response_count),
            "usage": dict(usage.__dict__),
        }
        for _attempt in range(2):
            try:
                self._client_with_timeout(
                    self.receipt_timeout_seconds
                ).request_sync("llm_usage_receipt", payload)
                self.last_receipt_error = ""
                return
            except Exception as exc:
                # The receipt is request-id idempotent, so a lost acknowledgement
                # can be retried without double-accounting. Receipt failure must
                # never turn a completed provider call into a generation retry.
                self.last_receipt_error = f"{type(exc).__name__}: {exc}"

    def refresh_credentials(self) -> bool:
        return False

    def activate_endpoint(self, _endpoint_id: str) -> None:
        return None

    def close(self) -> None:
        with self._lock:
            streams = tuple(self._active_streams)
            self._active_streams.clear()
        for stream in streams:
            stream.close()

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from pal.foundation.fd_lease import FdCloseOutcome, FdLease
from pal.foundation.sidecar import SidecarRpcError
from pal.llm.endpoint_spec import endpoint_spec_fingerprint
from pal.llm.ir import LLMUsageIR
from pal.llm.models import LLMEndpointModel
from pal.llm.shapes.base import _JSONFrame
from pal.llm.transport import (
    EncodedTransportRequest,
    LLMEndpointSpecStaleError,
    LLMProviderStartedError,
    LLMStreamControl,
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
    _active_connections: dict[str, LLMStreamControl] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
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
        control = request.stream_control or LLMStreamControl()
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
        stream_resource = _ProxyStreamResource()
        owner = FdLease(
            resource_kind=f"llm.manager_proxy:{endpoint.endpoint_id}",
            _resource=stream_resource,
            capacity=1,
            closer_sync=_close_proxy_stream,
            hard_closer_sync=_close_proxy_stream,
        )
        capability = owner.acquire(
            operation_id=request.request_id,
            interrupt=_interrupt_proxy_stream,
        )
        control.bind(capability)
        with self._lock:
            self._active_connections[owner.owner_id] = control
        succeeded = False
        provider_started = False
        try:
            control.raise_if_cancelled()
            capability.call_sync(
                lambda resource: _open_proxy_stream(
                    resource,
                    self._client,
                    params,
                )
            )
            expected_sequence = 0
            while True:
                try:
                    item = capability.call_sync(_next_proxy_stream_item)
                except StopIteration:
                    break
                event = str(item.get("event") or "")
                if event == "provider_started":
                    provider_started = True
                    control.mark_provider_started()
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
                control.touch_network()
                control.raise_if_cancelled()
                yield _JSONFrame(sequence, dict(payload))
                expected_sequence += 1
            control.raise_if_cancelled()
            succeeded = True
        except SidecarRpcError as exc:
            if control.cancelled:
                control.raise_if_cancelled()
            provider_started = bool(
                provider_started
                or dict(exc.payload or {}).get("provider_started")
                or control.provider_started
            )
            if provider_started:
                control.touch_network()
            if exc.kind == "endpoint_spec_stale" and not provider_started:
                raise LLMEndpointSpecStaleError(str(exc)) from exc
            if provider_started:
                raise LLMProviderStartedError(str(exc)) from exc
            raise LLMTransportError(str(exc)) from exc
        finally:
            capability.release_sync(reuse=False)
            control.unbind(capability)
            with self._lock:
                self._active_connections.pop(owner.owner_id, None)

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
            controls = tuple(self._active_connections.values())
        for control in controls:
            control.cancel("proxy_transport_close")


@dataclass
class _ProxyStreamResource:
    stream: Any = None


def _interrupt_proxy_stream(resource: _ProxyStreamResource, _reason: str) -> None:
    stream = resource.stream
    interrupt = getattr(stream, "interrupt", None)
    if callable(interrupt):
        interrupt()


def _close_proxy_stream(resource: _ProxyStreamResource) -> FdCloseOutcome:
    stream = resource.stream
    if stream is None:
        return FdCloseOutcome.detached()
    stream.close()
    return FdCloseOutcome.detached()


def _open_proxy_stream(
    resource: _ProxyStreamResource,
    client: BunshinManagerClient | BunshinRoleGatewayClient,
    params: dict[str, Any],
) -> None:
    if resource.stream is not None:
        raise RuntimeError("manager proxy stream is already open")
    resource.stream = client.stream_sync("llm_transport_stream", params)


def _next_proxy_stream_item(resource: _ProxyStreamResource) -> dict[str, Any]:
    if resource.stream is None:
        raise RuntimeError("manager proxy stream is not open")
    return next(resource.stream)

"""Host-side transport and usage accounting for already-authorized Bunshin LLM calls."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict, deque
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from pal.llm.credentials import LLMCredentialResolver
from pal.llm.ir import LLMUsageIR
from pal.llm.models import LLMEndpointModel
from pal.llm.repository import LLMEndpointRepository
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.llm.transport import DirectEndpointTransport
from pal.llm.transport_contracts import CloseableFrameIterator, EncodedTransportRequest, LLMStreamControl
from pal.llm.shapes.base import _JSONFrame
from pal.llm.attempts import LLMAttemptResult
from pal.foundation.workers import join_owned_task
from pal.foundation.sidecar import pack_sidecar_message
from pal.llm.chatgpt import exception_error
from pal.llm.usage import LLMUsageLedger
from pal.shared.json_values import thaw_json


_LLM_TRANSPORT_REQUEST_TTL_SECONDS = 30.0 * 60.0
_LLM_TRANSPORT_REQUEST_MAX_ENTRIES = 2048


class LLMProxyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: str = "manager_transport",
        provider_started: bool = False,
        chatgpt_failure: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.chatgpt_failure = chatgpt_failure
        self.kind = kind
        self.provider_started = bool(provider_started)

    @classmethod
    def from_transport(cls, exc: Exception, control: LLMStreamControl) -> LLMProxyError:
        failure = exception_error(exc)
        return cls(str(exc), provider_started=control.provider_started, chatgpt_failure=failure.to_dict() if failure else None)


@dataclass
class _LLMTransportRequestState:
    request_id: str
    run_id: str
    attempt_id: str
    endpoint_id: str
    model_id: str
    provider: str
    created_at: float = field(default_factory=time.monotonic)
    provider_started: bool = False
    transport_terminal: bool = False
    usage_received: bool = False
    receipt_fingerprint: str = ""


@dataclass(frozen=True)
class _TransportWorkerResult:
    error: BaseException | None = None
    close_error: Exception | None = None


def _consume_transport_iterator(
    iterator: Iterator[_JSONFrame],
    publish: Callable[[_JSONFrame | _TransportWorkerResult], bool],
) -> _TransportWorkerResult:
    """Consume and close one provider iterator on one stable owner thread."""

    error: BaseException | None = None
    close_error: Exception | None = None
    try:
        for frame in iterator:
            if not publish(frame):
                break
    except BaseException as exc:
        error = exc
    if isinstance(iterator, CloseableFrameIterator):
        try:
            iterator.close()
        except Exception as exc:
            close_error = exc
    result = _TransportWorkerResult(error=error, close_error=close_error)
    publish(result)
    return result


def _normalize_usage_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    integer_fields = (
        "input_tokens",
        "uncached_input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_tokens",
    )
    for key in integer_fields:
        try:
            parsed = int(value.get(key, 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"LLM usage receipt {key} must be an integer") from exc
        if parsed < 0:
            raise ValueError(f"LLM usage receipt {key} must be non-negative")
        normalized[key] = parsed
    try:
        cost = float(value.get("cost", 0.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("LLM usage receipt cost must be numeric") from exc
    if cost < 0:
        raise ValueError("LLM usage receipt cost must be non-negative")
    normalized["cost"] = cost
    normalized["reported"] = bool(value.get("reported", False))
    normalized["reasoning_tokens_reported"] = bool(
        value.get("reasoning_tokens_reported", False)
    )
    fields = value.get("reported_fields", ())
    allowed = {*integer_fields, "cost"}
    if not isinstance(fields, (list, tuple)) or any(item not in allowed for item in fields):
        raise ValueError("invalid usage field-presence map")
    normalized["reported_fields"] = tuple(fields)
    accounting = value.get("input_accounting", "unknown")
    if accounting not in {"unknown", "inclusive_cache", "exclusive_cache", "aggregate"}:
        raise ValueError("invalid input accounting")
    normalized["input_accounting"] = accounting
    normalized["final"] = bool(value.get("final", False))
    normalized["usage_anomaly"] = str(value.get("usage_anomaly") or "")[:256]
    raw_input = value.get("raw_input_tokens")
    normalized["raw_input_tokens"] = int(raw_input) if raw_input is not None else None
    raw = value.get("raw_counters", ())
    from pal.llm.usage_normalization import USAGE_FIELD_PATHS
    import math
    paths = {path for entries in USAGE_FIELD_PATHS.values() for path in entries}
    if not isinstance(raw, (list, tuple)) or len(raw) > len(paths):
        raise ValueError("invalid raw usage counters")
    counters = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2 or item[0] not in paths:
            raise ValueError("invalid raw usage counter")
        if not isinstance(item[1], (int, float)) or not math.isfinite(item[1]):
            raise ValueError("raw usage counter must be finite")
        counters.append(tuple(item))
    normalized["raw_counters"] = tuple(counters)
    return normalized


@dataclass
class LLMProxy:
    runtime_root: Path
    transport: DirectEndpointTransport | None = field(default=None, repr=False)
    requests: OrderedDict[str, _LLMTransportRequestState] = field(default_factory=OrderedDict, init=False, repr=False)
    workers: set[asyncio.Task[_TransportWorkerResult]] = field(default_factory=set, init=False, repr=False)
    usage: LLMUsageLedger = field(default_factory=lambda: LLMUsageLedger(scope="bunshin_manager_transport"), init=False)
    _retire_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _retiring: deque[tuple[DirectEndpointTransport, tuple[asyncio.Task[_TransportWorkerResult], ...]]] = field(
        default_factory=deque, init=False, repr=False,
    )

    async def stream(
        self,
        run_id: str,
        attempt_id: str,
        endpoint: LLMEndpointModel,
        request: EncodedTransportRequest,
    ) -> AsyncGenerator[dict[str, Any], None]:
        request_id = request.request_id
        self._prune_requests()
        if request_id in self.requests:
            raise LLMProxyError(
                f"LLM transport request_id was already used: {request_id}",
                kind="request_replay",
            )
        record = _LLMTransportRequestState(
            request_id=request_id,
            run_id=run_id,
            attempt_id=attempt_id,
            endpoint_id=str(endpoint.endpoint_id),
            model_id=str(endpoint.model_id),
            provider=str(endpoint.provider),
        )
        self.requests[request_id] = record
        control = LLMStreamControl()
        try:
            transport = self._endpoint_transport()
            iterator = transport.frames(endpoint, replace(request, stream_control=control))
        except Exception as exc:
            record.transport_terminal = True
            record.provider_started = control.provider_started
            raise LLMProxyError.from_transport(exc, control) from exc
        provider_announced = False
        completed = False
        loop = asyncio.get_running_loop()
        events: asyncio.Queue[tuple[_JSONFrame | _TransportWorkerResult, threading.Event | None]] = asyncio.Queue()

        def publish(value: _JSONFrame | _TransportWorkerResult) -> bool:
            is_frame = not isinstance(value, _TransportWorkerResult)
            if is_frame and control.cancelled:
                return False
            acknowledged = threading.Event() if is_frame else None
            try:
                loop.call_soon_threadsafe(
                    events.put_nowait,
                    (value, acknowledged),
                )
            except RuntimeError:
                return False
            if acknowledged is not None:
                while not acknowledged.wait(0.05):
                    if control.cancelled:
                        return False
            return True

        worker = asyncio.create_task(
            asyncio.to_thread(_consume_transport_iterator, iterator, publish),
            name=f"bunshin-llm-transport:{request_id}",
        )
        self.workers.add(worker)

        def worker_finished(task: asyncio.Task[_TransportWorkerResult]) -> None:
            self.workers.discard(task)
            if task.cancelled():
                return
            with contextlib.suppress(BaseException):
                task.result()
                record.provider_started = bool(
                    record.provider_started or control.provider_started
                )
                record.transport_terminal = True

        worker.add_done_callback(worker_finished)
        pending_acknowledgement: threading.Event | None = None
        try:
            while True:
                try:
                    value, acknowledgement = await asyncio.wait_for(
                        events.get(),
                        timeout=0.05,
                    )
                except TimeoutError:
                    if control.provider_started and not provider_announced:
                        provider_announced = True
                        record.provider_started = True
                        yield {"event": "provider_started"}
                    continue
                if control.provider_started and not provider_announced:
                    provider_announced = True
                    record.provider_started = True
                    yield {"event": "provider_started"}
                if isinstance(value, _TransportWorkerResult):
                    result = value
                    completed = True
                    await asyncio.shield(worker)
                    if result.error is not None:
                        raise result.error
                    if result.close_error is not None:
                        raise result.close_error
                    return
                frame = value
                pending_acknowledgement = acknowledgement
                try:
                    yield {
                        "frame": {
                            "sequence": int(frame.sequence),
                            "payload": thaw_json(frame.payload),
                        }
                    }
                finally:
                    if acknowledgement is not None:
                        acknowledgement.set()
                    pending_acknowledgement = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            record.provider_started = bool(
                record.provider_started or control.provider_started
            )
            raise LLMProxyError.from_transport(exc, control) from exc
        finally:
            if pending_acknowledgement is not None:
                pending_acknowledgement.set()
            if not completed:
                control.cancel("proxy_consumer_closed")
            if not worker.done():
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(asyncio.shield(worker), timeout=1.0)
            record.provider_started = bool(
                record.provider_started or control.provider_started
            )
            if worker.done() and not worker.cancelled():
                record.transport_terminal = True

    def record_usage(self, run_id: str, attempt_id: str, params: Mapping[str, Any]) -> dict[str, Any]:
        self._prune_requests()
        request_id = str(params.get("request_id") or "").strip()
        record = self.requests.get(request_id)
        if record is None or record.run_id != run_id or record.attempt_id != attempt_id:
            raise KeyError(f"unknown LLM transport request receipt: {request_id}")
        if not record.transport_terminal:
            raise RuntimeError("LLM usage receipt arrived before transport terminal")
        endpoint_id = str(params.get("endpoint_id") or "").strip()
        if endpoint_id != record.endpoint_id:
            raise PermissionError("LLM usage receipt changed endpoint identity")
        usage_payload = params.get("usage")
        if not isinstance(usage_payload, Mapping):
            raise ValueError("LLM usage receipt has no usage object")
        normalized_usage = _normalize_usage_receipt(usage_payload)
        try:
            provider_response_count = int(
                params.get("provider_response_count") or 0
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "LLM usage receipt provider_response_count must be an integer"
            ) from exc
        if provider_response_count < 1:
            raise ValueError(
                "LLM usage receipt provider_response_count must be positive"
            )
        status = str(params.get("status") or "success")
        if status not in {"success", "failed", "cancelled"}:
            raise ValueError("invalid attempt settlement status")
        attempt = LLMAttemptResult(
            attempt_id=request_id, endpoint_id=endpoint_id, model_id=str(params.get("model_id") or ""),
            provider=str(params.get("provider") or ""), status=status, usage=LLMUsageIR(**normalized_usage),
            provider_generation_id=str(params.get("provider_generation_id") or ""),
            returned_model=str(params.get("returned_model") or ""), actual_provider=str(params.get("actual_provider") or ""),
            service_tier=str(params.get("service_tier") or ""),
        )
        receipt_payload = {
            "status": attempt.status, "provider_generation_id": attempt.provider_generation_id,
            "returned_model": attempt.returned_model, "actual_provider": attempt.actual_provider,
            "service_tier": attempt.service_tier, "request_id": request_id, "endpoint_id": endpoint_id,
            "model_id": attempt.model_id, "provider": attempt.provider,
            "provider_response_count": provider_response_count, "usage": normalized_usage,
        }
        receipt_fingerprint = hashlib.sha256(
            json.dumps(
                receipt_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if record.usage_received:
            if record.receipt_fingerprint != receipt_fingerprint:
                raise ValueError("LLM usage receipt replay changed its payload")
            return {"ok": True, "duplicate": True}
        if attempt.model_id != record.model_id:
            raise ValueError("LLM usage receipt changed model identity")
        if attempt.provider != record.provider:
            raise ValueError("LLM usage receipt changed provider identity")
        if "status" in params:
            self.usage.record_attempt(attempt)
        if status == "success":
            self.usage.record_success(
                endpoint_id=record.endpoint_id, model_id=record.model_id, provider=record.provider,
                usage=attempt.usage, provider_response_count=provider_response_count,
                usage_accounted="status" in params,
            )
        record.usage_received = True
        record.receipt_fingerprint = receipt_fingerprint
        return {"ok": True, "duplicate": False}

    def _endpoint_transport(self) -> DirectEndpointTransport:
        if self.transport is None:
            credentials = LLMCredentialResolver(
                secret_store=EncryptedFileSecretStore(
                    secrets_path=str(Path(self.runtime_root) / "secrets.json")
                )
            )
            transport = DirectEndpointTransport(
                credential_resolver=credentials.resolve_api_key
            )
            transport.prepare_endpoints(LLMEndpointRepository().list_enabled())
            self.transport = transport
        return self.transport

    def _prune_requests(self) -> None:
        now = time.monotonic()
        for request_id, record in tuple(self.requests.items()):
            if (
                record.transport_terminal
                and now - record.created_at > _LLM_TRANSPORT_REQUEST_TTL_SECONDS
            ):
                self.requests.pop(request_id, None)
        while (
            len(self.requests)
            > _LLM_TRANSPORT_REQUEST_MAX_ENTRIES
        ):
            terminal_id = next(
                (
                    request_id
                    for request_id, record in self.requests.items()
                    if record.transport_terminal
                ),
                "",
            )
            if not terminal_id:
                break
            self.requests.pop(terminal_id, None)

    async def retire_transport(self) -> bool:
        async with self._retire_lock:
            if self.transport is not None:
                self._retiring.append((self.transport, tuple(self.workers)))
                self.transport = None
            if not self._retiring:
                return False
            await join_owned_task(asyncio.create_task(self._close_retiring()))
            return True

    async def _close_retiring(self) -> None:
        while self._retiring:
            transport, workers = self._retiring[0]
            # DirectEndpointTransport.frames binds lazily on its owner thread.
            # Keep this generation alive until all admitted iterators have closed.
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
            await asyncio.to_thread(transport.close)
            self._retiring.popleft()

    def health(self) -> dict[str, Any]:
        self._prune_requests()
        return {
            "llm_transport_request_count": len(self.requests),
            "llm_transport_worker_count": len(self.workers),
            "llm_usage_unreported_count": sum(
                item.transport_terminal and not item.usage_received for item in self.requests.values()
            ),
            "llm_usage": self.usage.snapshot(),
        }


async def serve_llm_stream(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    open_stream: Callable[[], Awaitable[AsyncGenerator[dict[str, Any], None]]],
    *, request_id: str, error_kind: str, logger: logging.Logger,
) -> None:
    """Forward frames until completion or peer disconnect, joining the pending read before closing."""
    try:
        async with contextlib.aclosing(await open_stream()) as stream:
            disconnect = asyncio.create_task(reader.read(1))
            advance: asyncio.Task[dict[str, Any]] | None = None
            try:
                while True:
                    advance = asyncio.create_task(anext(stream))
                    done, _ = await asyncio.wait({advance, disconnect}, return_when=asyncio.FIRST_COMPLETED)
                    if disconnect in done:
                        return
                    try:
                        item = advance.result()
                    except StopAsyncIteration:
                        break
                    advance = None
                    writer.write(pack_sidecar_message({
                        "type": "stream_item", "id": request_id, "ok": True, "result": item,
                    }))
                    await writer.drain()
            finally:
                disconnect.cancel()
                if advance is not None:
                    advance.cancel()
                    await asyncio.gather(advance, return_exceptions=True)
                await asyncio.gather(disconnect, return_exceptions=True)
        terminal = {"type": "stream_end", "id": request_id, "ok": True, "result": {}}
    except Exception as exc:
        logger.exception("sidecar LLM stream request failed")
        failure = exc if isinstance(exc, LLMProxyError) else None
        terminal = {"type": "stream_end", "id": request_id, "ok": False, "error": {
            "kind": failure.kind if failure else error_kind,
            "message": f"{exc.__class__.__name__}: {exc}",
            "provider_started": failure.provider_started if failure else False,
            "chatgpt_failure": failure.chatgpt_failure if failure else None,
        }}
    writer.write(pack_sidecar_message(terminal))
    await writer.drain()

"""Durable ingress staging for channel turns awaiting admission.

The in-memory ``pending_channel_turns`` deque stays the live working queue;
this store is its durable shadow plus the event-receipt ledger used for
deduplication after the original transcript text has been compacted away
(Q08/R03: dedup keys on the source event id, never on transcript content).

Persistence follows the resident checkpoint pattern: one JSON document per
runtime root, replaced atomically (tmp file + fsync + os.replace + directory
fsync) on every mutation. Enqueue durability is synchronous — a caller may
only acknowledge "queued" after ``enqueue`` returns (Q11).

Hard-crash honesty: the file is rewritten on each mutation, so a crash can
lose only the window since the last completed write. Message conservation
across that window is the transport's redelivery business (the channel
event was never acked); the store itself never silently drops entries
(Q10: full means refuse, never pop-the-oldest).
"""
from __future__ import annotations

import json
import os
from uuid import uuid4
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from pal.foundation import utc_now
from pal.llm.ir import LLMMessageIR
from pal.llm.serde import message_from_payload, message_to_payload

DEFAULT_MAX_ENTRIES = 64
DEFAULT_MAX_BYTES = 4 * 1024 * 1024  # envelope metadata budget, not payloads
RECEIPT_RETENTION = 512

_SCHEMA = 1


class IngressStagingError(RuntimeError):
    """Staging persistence failed; nothing was acknowledged."""


class IngressStagingFull(IngressStagingError):
    """Bounded queue is full; the caller must not acknowledge the message."""


@dataclass(frozen=True)
class StagedIngressRecord:
    scope: str
    event_id: str
    event_kind: str
    source_kind: str
    correlation_id: str | None
    created_at: str
    endpoint_id: str
    channel_kind: str
    binding_key: str
    send_policy: dict[str, Any]
    response_endpoint_id: str
    reply_target: dict[str, Any]
    payload_kind: str  # "json" | "message_ir"
    payload: Any
    binding_control_scope_key: str | None = None
    binding_correlation_id: str | None = None
    queued_at: str = field(default_factory=utc_now)

    def to_payload(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "event_id": self.event_id,
            "event_kind": self.event_kind,
            "source_kind": self.source_kind,
            "correlation_id": self.correlation_id,
            "created_at": self.created_at,
            "endpoint_id": self.endpoint_id,
            "channel_kind": self.channel_kind,
            "binding_key": self.binding_key,
            "send_policy": dict(self.send_policy or {}),
            "response_endpoint_id": self.response_endpoint_id,
            "reply_target": dict(self.reply_target or {}),
            "payload_kind": self.payload_kind,
            "payload": self.payload,
            "binding_control_scope_key": self.binding_control_scope_key,
            "binding_correlation_id": self.binding_correlation_id,
            "queued_at": self.queued_at,
        }

    @classmethod
    def from_payload(cls, value: Mapping[str, Any]) -> "StagedIngressRecord":
        allowed = {
            "scope", "event_id", "event_kind", "source_kind", "correlation_id",
            "created_at", "endpoint_id", "channel_kind", "binding_key",
            "send_policy", "response_endpoint_id", "reply_target",
            "payload_kind", "payload", "binding_control_scope_key",
            "binding_correlation_id", "queued_at",
        }
        if extras := sorted(set(value) - allowed):
            raise IngressStagingError(
                f"ingress staging record has unknown fields: {extras}")
        payload_kind = str(value.get("payload_kind") or "")
        if payload_kind not in {"json", "message_ir"}:
            raise IngressStagingError(
                "ingress staging record has an unsupported payload_kind")
        return cls(
            scope=str(value.get("scope") or ""),
            event_id=str(value.get("event_id") or ""),
            event_kind=str(value.get("event_kind") or ""),
            source_kind=str(value.get("source_kind") or ""),
            correlation_id=value.get("correlation_id"),
            created_at=str(value.get("created_at") or ""),
            endpoint_id=str(value.get("endpoint_id") or ""),
            channel_kind=str(value.get("channel_kind") or ""),
            binding_key=str(value.get("binding_key") or ""),
            send_policy=dict(value.get("send_policy") or {}),
            response_endpoint_id=str(value.get("response_endpoint_id") or ""),
            reply_target=dict(value.get("reply_target") or {}),
            payload_kind=payload_kind,
            payload=value.get("payload"),
            binding_control_scope_key=value.get("binding_control_scope_key"),
            binding_correlation_id=value.get("binding_correlation_id"),
            queued_at=str(value.get("queued_at") or ""),
        )

    def to_channel_envelope(self):
        from pal.foundation.io import EventEnvelope
        from pal.shared.agent_io import (
            ChannelEnvelope,
            EndpointConfig,
            ResponseHandle,
            TurnDeliveryBinding,
        )

        payload: Any = self.payload
        if self.payload_kind == "message_ir":
            payload = message_from_payload(self.payload)
        binding = None
        if self.binding_control_scope_key:
            binding = TurnDeliveryBinding(
                endpoint=EndpointConfig(
                    endpoint_id=self.endpoint_id,
                    channel_kind=self.channel_kind,
                    binding_key=self.binding_key,
                    send_policy=dict(self.send_policy or {}),
                ),
                response_handle=ResponseHandle(
                    endpoint_id=self.response_endpoint_id,
                    reply_target=dict(self.reply_target or {}),
                ),
                control_scope_key=str(self.binding_control_scope_key),
                correlation_id=self.binding_correlation_id,
            )
        return ChannelEnvelope(
            event=EventEnvelope(
                event_kind=self.event_kind,
                source_kind=self.source_kind,
                payload=payload,
                correlation_id=self.correlation_id,
                created_at=self.created_at,
                event_id=self.event_id,
            ),
            endpoint=EndpointConfig(
                endpoint_id=self.endpoint_id,
                channel_kind=self.channel_kind,
                binding_key=self.binding_key,
                send_policy=dict(self.send_policy or {}),
            ),
            response_handle=ResponseHandle(
                endpoint_id=self.response_endpoint_id,
                reply_target=dict(self.reply_target or {}),
            ),
            opening_delivery_binding=binding,
        )

    @classmethod
    def from_channel_envelope(
        cls,
        envelope: Any,
        *,
        scope: str,
    ) -> "StagedIngressRecord":
        event = envelope.event
        payload = getattr(event, "payload", None)
        if isinstance(payload, LLMMessageIR):
            payload_kind, stored = "message_ir", message_to_payload(payload)
        elif payload is None or isinstance(payload, (str, int, float, bool)):
            payload_kind, stored = "json", payload
        elif isinstance(payload, Mapping):
            payload_kind, stored = "json", dict(payload)
        else:
            raise IngressStagingError(
                f"cannot stage event payload of type {type(payload).__name__}")
        binding = getattr(envelope, "opening_delivery_binding", None)
        endpoint = envelope.endpoint
        return cls(
            scope=str(scope),
            event_id=str(event.event_id),
            event_kind=str(event.event_kind),
            source_kind=str(event.source_kind),
            correlation_id=event.correlation_id,
            created_at=str(getattr(event, "created_at", "") or ""),
            endpoint_id=str(endpoint.endpoint_id),
            channel_kind=str(endpoint.channel_kind),
            binding_key=str(endpoint.binding_key),
            send_policy=dict(endpoint.send_policy or {}),
            response_endpoint_id=str(envelope.response_handle.endpoint_id),
            reply_target=dict(envelope.response_handle.reply_target or {}),
            payload_kind=payload_kind,
            payload=stored,
            binding_control_scope_key=(
                str(binding.control_scope_key) if binding is not None else None
            ),
            binding_correlation_id=(
                binding.correlation_id if binding is not None else None
            ),
        )


def _atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    target = Path(path)
    temporary: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.parent / f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp"
        stream = temporary.open("w", encoding="utf-8")
        try:
            json.dump(document, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        finally:
            stream.close()
        os.replace(temporary, target)
        descriptor = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except IngressStagingError:
        raise
    except Exception as exc:  # persistence failure must never look like success
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        raise IngressStagingError(f"ingress staging write failed: {exc}") from exc


class IngressStagingStore:
    """Bounded durable FIFO shadow for one host's pending channel turns."""

    def __init__(
        self,
        path: str | Path,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.path = Path(path)
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1024, int(max_bytes))
        self._document: dict[str, Any] | None = None

    # ── document handling ─────────────────────────────────────────────

    def _load_document(self) -> dict[str, Any]:
        if self._document is not None:
            return self._document
        if not self.path.exists():
            self._document = {"schema": _SCHEMA, "pending": [], "receipts": {}}
            return self._document
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except IngressStagingError:
            raise
        except Exception as exc:
            raise IngressStagingError(
                f"ingress staging store is unreadable: {exc}") from exc
        if not isinstance(raw, dict) or int(raw.get("schema") or 0) != _SCHEMA:
            raise IngressStagingError(
                "ingress staging store schema is unsupported; refusing to "
                "reinterpret an unknown format")
        allowed = {"schema", "pending", "receipts"}
        if extras := sorted(set(raw) - allowed):
            raise IngressStagingError(
                f"ingress staging store has unknown fields: {extras}")
        self._document = {
            "schema": _SCHEMA,
            "pending": list(raw.get("pending") or []),
            "receipts": dict(raw.get("receipts") or {}),
        }
        return self._document

    def _flush(self) -> None:
        document = self._load_document()
        _atomic_write_json(self.path, document)

    def _pending_records(self) -> list[StagedIngressRecord]:
        document = self._load_document()
        return [StagedIngressRecord.from_payload(item) for item in document["pending"]]

    def _replace_pending(self, records: list[StagedIngressRecord]) -> None:
        document = self._load_document()
        document["pending"] = [record.to_payload() for record in records]
        self._flush()

    # ── enqueue / inspect (queue plane) ───────────────────────────────

    def enqueue(self, record: StagedIngressRecord) -> None:
        """Durably append one pending record; refuse duplicates and overflow.

        Raises:
            IngressStagingFull: bounded capacity reached — do not acknowledge.
            IngressStagingError: duplicate event id already pending/receipted,
                or persistence failed.
        """
        document = self._load_document()
        pending_ids = {
            str(item.get("event_id") or "") for item in document["pending"]
        }
        if record.event_id in pending_ids or record.event_id in document["receipts"]:
            raise IngressStagingError(
                f"ingress event {record.event_id} is already pending or accepted")
        if len(document["pending"]) >= self.max_entries:
            raise IngressStagingFull(
                f"ingress staging is full ({self.max_entries} entries)")
        candidate = list(document["pending"]) + [record.to_payload()]
        try:
            encoded = json.dumps(candidate, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise IngressStagingError(
                f"ingress staging payload is not serializable: {exc}") from exc
        if len(encoded.encode("utf-8")) > self.max_bytes:
            raise IngressStagingFull(
                f"ingress staging is over its byte budget ({self.max_bytes})")
        document["pending"] = candidate
        self._flush()

    def pending_records(self, *, scope: str | None = None) -> tuple[StagedIngressRecord, ...]:
        records = self._pending_records()
        if scope is None:
            return tuple(records)
        return tuple(record for record in records if record.scope == str(scope))

    def remove(self, event_id: str) -> bool:
        records = self._pending_records()
        remaining = [record for record in records if record.event_id != str(event_id)]
        if len(remaining) == len(records):
            return False
        self._replace_pending(remaining)
        return True

    # ── receipts (dedup plane, independent of transcript text) ────────

    def record_receipt(self, event_id: str, *, turn_id: str) -> None:
        document = self._load_document()
        receipts = document["receipts"]
        receipts[str(event_id)] = {
            "turn_id": str(turn_id),
            "accepted_at": utc_now(),
        }
        if len(receipts) > RECEIPT_RETENTION:
            ordered = sorted(
                receipts.items(),
                key=lambda item: str(item[1].get("accepted_at") or ""),
            )
            for key, _ in ordered[: len(receipts) - RECEIPT_RETENTION]:
                receipts.pop(key, None)
        self._flush()

    def has_receipt(self, event_id: str) -> bool:
        return str(event_id) in self._load_document()["receipts"]

    def receipt_for(self, event_id: str) -> dict[str, Any] | None:
        value = self._load_document()["receipts"].get(str(event_id))
        return dict(value) if isinstance(value, Mapping) else None


__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_ENTRIES",
    "IngressStagingError",
    "IngressStagingFull",
    "IngressStagingStore",
    "StagedIngressRecord",
]

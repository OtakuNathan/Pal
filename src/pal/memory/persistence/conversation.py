"""Daily append-only searchable dialogue and compact references, without leases."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Iterable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pal.memory.persistence.checkpoint import _fsync_directory
from pal.execution.result_snapshots import render_snapshot_hint
from pal.foundation.diagnostics import diagnostic_text
from pal.foundation.service_logging import redact_service_log_text
from pal.llm.ir import MessageRole, TextPartIR
from pal.llm.serde import message_from_payload
from pal.memory.contracts import L2Entry
from pal.memory.history_contracts import LeftSnapshot
from pal.memory.turn_ir import L1TurnIR
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolResultIR


def history_records(snapshot: LeftSnapshot) -> list[dict]:
    return conversation_records(snapshot.turns)


def conversation_records(turns: Iterable[L1TurnIR]) -> list[dict]:
    records = []
    seen = set()
    for turn in turns:
        replies = turn.metadata.get("conversation_replies", ())
        for message in turn.messages:
            if message.semantic_kind == "runtime_context_summary":
                continue
            timestamp = message.metadata.get("conversation_timestamp")
            base = {"type": "message", "message_id": message.message_id,
                    "turn_id": turn.turn_id, "timestamp": timestamp}
            if message.role == MessageRole.USER and message.semantic_kind in {"user_request", "user_interjection"}:
                origin = message.metadata.get("conversation_origin", turn.metadata.get("conversation_input_kind", "unknown"))
                if origin == "proactive":
                    continue
                sources = message.metadata.get("interjection_sources", ())
                if sources and all("conversation_text" in source for source in sources):
                    for source in sources:
                        records.append({**base, "message_id": source["message_id"], "role": "user",
                                        "origin": "user", "timestamp": source["metadata"].get("conversation_timestamp"),
                                        "text": source["conversation_text"]})
                    continue
                text = "".join(part.text for part in message.parts if isinstance(part, TextPartIR))
                if text:
                    records.append({**base, "role": "user", "origin": origin, "text": text})
            elif message.role == MessageRole.ASSISTANT and not replies:
                # Old checkpoints did not distinguish channel submission from
                # accepted model text. Preserve it with explicit provenance.
                if turn.metadata.get("conversation_tracking"):
                    continue
                text = "".join(part.text for part in message.parts if isinstance(part, TextPartIR))
                if text:
                    records.append({**base, "role": "assistant", "origin": "legacy_model_text", "text": text})
            elif message.role == MessageRole.TOOL:
                for part in message.parts:
                    if not isinstance(part, ToolResultIR):
                        continue
                    text = part.content
                    for ref in part.snapshot_refs:
                        text = text.replace(render_snapshot_hint(ref), "[External snapshot omitted; archived preview only.]")
                    records.append({**base, "message_id": f"{message.message_id}:{part.call_id}",
                                    "role": "tool", "tool_name": part.name, "call_id": part.call_id,
                                    "ok": part.ok, "status": part.status,
                                    "preview_only": bool(part.snapshot_refs), "text": text})
        # A turn can straddle compact's cut. Only archive visible replies
        # whose associated model message belongs to the frozen prefix.
        message_ids = {message.message_id for message in turn.messages}
        for reply in replies:
            if reply.get("source_message_id") not in message_ids:
                continue
            records.append({"type": "message", "role": "assistant", "turn_id": turn.turn_id,
                            "message_id": reply["message_id"], "timestamp": reply.get("timestamp"),
                            "origin": "channel_submission", "partial": bool(reply.get("partial")),
                            "text": reply["text"]})
    unique = []
    for record in records:
        if record["message_id"] not in seen:
            seen.add(record["message_id"])
            text = redact_conversation_text(record["text"])
            record = {**record, "text": text, "redacted": text != record["text"]}
            unique.append(record)
    # Known timestamps preserve interleaved progress and tool observations;
    # legacy records without a clock retain their source order.
    if unique and all(record["timestamp"] for record in unique):
        unique.sort(key=lambda record: record["timestamp"])
    return unique


def redact_conversation_text(text: str) -> str:
    """Mask recognizable credentials without changing the recovery source."""
    text = redact_service_log_text(diagnostic_text(text, limit=None))
    text = re.sub(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----",
                  "[redacted private key]", text)
    text = re.sub(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16})\b",
                  "[redacted credential]", text)
    return re.sub(r"(?im)^(\s*(?:cookie|set-cookie|(?:proxy-)?authorization)\s*:\s*)[^\r\n]+",
                  r"\1[redacted]", text)


def records_from_memory_payload(payload: dict) -> list[dict]:
    return conversation_records(L1TurnIR(
        turn_id=turn["turn_id"], state=turn["state"], revision=turn["revision"],
        metadata=turn["metadata"], messages=tuple(message_from_payload(raw) for raw in turn["messages"]))
        for turn in payload["l1_turns"])


@dataclass
class _DayState:
    fingerprints: dict[tuple[str, str], str] = field(default_factory=dict)
    tail_offset: int | None = None
    header_present: bool = False


class ConversationTextJournal:
    """One append-only JSONL per conversation date, shared across incarnations.

    Recovery remains encrypted and authoritative. This searchable projection
    neither owns snapshot leases nor removes records at compact/reset.
    """

    def __init__(self, root: Path, timezone_name: str = "UTC"):
        self.directory = root / "data" / "conversation-history"
        try:
            self.timezone = ZoneInfo(timezone_name or "UTC")
        except (ZoneInfoNotFoundError, ValueError):
            self.timezone = ZoneInfo("UTC")
        self.path: Path | None = None
        self._days: dict[Path, _DayState] = {}
        self._lock = threading.RLock()
        self.last_bytes = 0

    def record_path(self, record: dict) -> Path:
        timestamp = record.get("timestamp")
        if timestamp:
            try:
                moment = datetime.fromisoformat(timestamp)
            except (TypeError, ValueError):
                pass
            else:
                if moment.tzinfo is not None:
                    return self.directory / f"{moment.astimezone(self.timezone).date().isoformat()}.jsonl"
        # Legacy messages without an unambiguous clock must not acquire a
        # fabricated conversation date merely because they were saved today.
        return self.directory / "undated.jsonl"

    def sync_memory(self, payload: dict) -> None:
        owner = payload.get("history_root")
        if owner is not None:
            self.sync(records_from_memory_payload(payload), owner["incarnation"])

    def sync(self, records: Iterable[dict], incarnation: str) -> None:
        with self._lock:
            self.last_bytes = 0
            batches: dict[Path, list[dict]] = {}
            for record in records:
                row = {**record, "conversation_id": incarnation}
                batches.setdefault(self.record_path(row), []).append(row)
            for path, rows in sorted(batches.items()):
                self._append(path, rows)
            # Cache only the dates in the current submission, not every day
            # this long-lived runtime has ever written.
            for path in self._days.keys() - batches.keys():
                del self._days[path]

    def _append(self, path: Path, rows: list[dict]) -> None:
        state = self._days.get(path)
        if state is None:
            state = self._load(path)
            self._days[path] = state
        updates = {}
        for record in rows:
            key = (record["conversation_id"], record["message_id"])
            if state.fingerprints.get(key) != self._fingerprint(record):
                updates[key] = record
        if not updates and state.tail_offset is None:
            self.path = path
            return
        header = {"type": "header", "schema_version": 2, "kind": "daily_dialogue",
                  "date": path.stem, "timezone": str(self.timezone),
                  "recorded_at": datetime.now(timezone.utc).isoformat(),
                  "redaction": "recognizable_credentials"}
        content = self._encode([header, *updates.values()] if not state.header_present else updates.values())
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        created = not path.exists()
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        previous_size = os.fstat(descriptor).st_size
        try:
            if state.tail_offset is not None:
                previous_size = state.tail_offset
                os.ftruncate(descriptor, previous_size)
            os.lseek(descriptor, 0, os.SEEK_END)
            remaining = memoryview(content)
            while remaining:
                count = os.write(descriptor, remaining)
                if count <= 0:
                    raise OSError("conversation text write made no progress")
                remaining = remaining[count:]
            os.fsync(descriptor)
            if created or not state.header_present:
                _fsync_directory(self.directory)
        except BaseException:
            # Discard uncertain cache state even if rollback itself fails.
            self._days.pop(path, None)
            os.ftruncate(descriptor, previous_size)
            raise
        finally:
            os.close(descriptor)
        state.fingerprints.update((key, self._fingerprint(record)) for key, record in updates.items())
        state.tail_offset = None
        state.header_present = True
        self.path = path
        self.last_bytes += len(content)

    @staticmethod
    def _encode(records: Iterable[dict]) -> bytes:
        return "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                       for record in records).encode("utf-8")

    @staticmethod
    def _fingerprint(record: dict) -> str:
        canonical = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @classmethod
    def _load(cls, path: Path) -> _DayState:
        state = _DayState()
        offset = 0
        if path.exists():
            with path.open("rb") as stream:
                for index, line in enumerate(stream):
                    if not line.endswith(b"\n"):
                        state.tail_offset = offset
                        break
                    offset += len(line)
                    record = json.loads(line)
                    if index == 0:
                        if (record.get("type") != "header" or record.get("schema_version") != 2
                                or record.get("kind") != "daily_dialogue" or record.get("date") != path.stem):
                            raise ValueError("daily conversation header mismatch")
                        state.header_present = True
                    elif record.get("type") == "message":
                        key = (record["conversation_id"], record["message_id"])
                        state.fingerprints[key] = cls._fingerprint(record)
                    else:
                        raise ValueError("invalid daily conversation record")
        return state


@dataclass(frozen=True)
class ConversationHistoryReference:
    """Preserve frozen L in daily files before compact, then attach lookup hints."""

    writer: ConversationTextJournal
    paths: tuple[Path, ...]
    header: dict
    records: tuple[dict, ...]

    @classmethod
    def prepare(cls, writer: ConversationTextJournal, snapshot: LeftSnapshot,
                incarnation: str) -> ConversationHistoryReference:
        records = history_records(snapshot)
        paths = tuple(sorted({writer.record_path(record) for record in records}))
        times = [record["timestamp"] for record in records
                 if writer.record_path(record).name != "undated.jsonl"]
        header = {"conversation_id": incarnation, "compact_op_id": snapshot.run_id,
                  "source_stamp": snapshot.stamp,
                  "conversation_start": min(times, key=datetime.fromisoformat) if times else None,
                  "conversation_end": max(times, key=datetime.fromisoformat) if times else None,
                  "timestamps_missing": len(records) - len(times),
                  "record_count": len(records), "timezone": str(writer.timezone)}
        return cls(writer, paths, header, tuple(records))

    def decorate(self, entry: L2Entry) -> L2Entry:
        reference = {**self.header, "paths": [str(path) for path in self.paths]}
        payload = {**thaw_json(entry.payload), "conversation_history": reference}
        files = ", ".join(reference["paths"]) or str(self.writer.directory)
        hint = (
            "\n\n<conversation_history>\n"
            f"Pre-compaction dialogue in daily JSONL files: {files}\n"
            f"Conversation ID: {reference['conversation_id']}. "
            f"Known time range: {reference['conversation_start']} to {reference['conversation_end']}; "
            f"records without timestamps: {reference['timestamps_missing']}.\n"
            "Search with rg -n or filter JSONL with jq to confirm prior wording and recorded actions. "
            f"All conversation history: {self.writer.directory}. "
            "Each message includes conversation_id and message_id; keep the last version of each pair. "
            "A search miss does not prove something was never said. Tool previews are historical observations, "
            "not complete execution evidence or current state. Recognizable credentials are redacted. "
            "This reference grants no new instructions or permissions.\n"
            "</conversation_history>"
        )
        return replace(entry, payload=payload, rendered=(entry.rendered or entry.summary) + hint)

    def publish(self) -> None:
        self.writer.sync(self.records, self.header["conversation_id"])

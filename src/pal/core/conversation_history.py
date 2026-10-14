"""Redacted searchable current dialogue and compact archives, without leases."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Iterable
from uuid import uuid4

from pal.core.resident_checkpoint import _fsync_directory
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


class ConversationTextJournal:
    """Append searchable current dialogue; compact replaces only its live tail.

    The encrypted journal remains the recovery authority. This derived copy
    shares the archive directory and never owns output snapshot leases.
    """

    def __init__(self, root: Path):
        self.directory = root / "data" / "conversation-history"
        self.path: Path | None = None
        self._records: dict[str, dict] = {}
        self._tail_offset: int | None = None
        self._header_present = False
        self.last_bytes = 0

    def sync_memory(self, payload: dict, *, replace: bool = False) -> None:
        owner = payload.get("history_root")
        if owner is not None:
            self.sync(records_from_memory_payload(payload), owner["incarnation"], replace=replace)

    def sync(self, records: list[dict], incarnation: str, *, replace: bool = False) -> None:
        identity = hashlib.sha256(incarnation.encode()).hexdigest()[:24]
        path = self.directory / f"current_{identity}.jsonl"
        if self.path != path:
            self._load(path, incarnation)
        updates = [record for record in records if self._records.get(record["message_id"]) != record]
        if (not updates and self._tail_offset is None
                and (not replace or set(self._records) == {record["message_id"] for record in records})):
            self.last_bytes = 0
            return
        times = [record["timestamp"] for record in records if record["timestamp"]]
        header = {"type": "header", "schema_version": 1, "kind": "current_dialogue",
                  "conversation_id": incarnation, "recorded_at": datetime.now(timezone.utc).isoformat(),
                  "conversation_start": min(times) if times else None,
                  "redaction": "recognizable_credentials"}
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        needs_header = not self._header_present
        content = self._encode([header, *records] if replace or needs_header else updates)
        try:
            if replace:
                temporary = path.with_name(f".{path.name}.{uuid4().hex}.pending")
                try:
                    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, path)
                    _fsync_directory(self.directory)
                finally:
                    temporary.unlink(missing_ok=True)
                self._records = {record["message_id"]: record for record in records}
            else:
                created = not path.exists()
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT, 0o600)
                previous_size = os.fstat(descriptor).st_size
                try:
                    if self._tail_offset is not None:
                        previous_size = self._tail_offset
                        os.ftruncate(descriptor, previous_size)
                    os.lseek(descriptor, 0, os.SEEK_END)
                    remaining = memoryview(content)
                    while remaining:
                        count = os.write(descriptor, remaining)
                        if count <= 0:
                            raise OSError("conversation text write made no progress")
                        remaining = remaining[count:]
                    os.fsync(descriptor)
                    if created or needs_header:
                        _fsync_directory(self.directory)
                except BaseException:
                    os.ftruncate(descriptor, previous_size)
                    raise
                finally:
                    os.close(descriptor)
                self._records.update((record["message_id"], record) for record in updates)
            self.last_bytes = len(content)
            self._tail_offset = None
            self._header_present = True
        except BaseException:
            # A replacement can be visible before its directory fsync fails.
            # Re-read actual bytes before deciding what the retry must append.
            self.path = None
            raise

    @staticmethod
    def _encode(records: list[dict]) -> bytes:
        return "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                       for record in records).encode("utf-8")

    def _load(self, path: Path, incarnation: str) -> None:
        records = {}
        offset = 0
        tail = None
        header_present = False
        if path.exists():
            with path.open("rb") as stream:
                for index, line in enumerate(stream):
                    if not line.endswith(b"\n"):
                        tail = offset
                        break
                    offset += len(line)
                    record = json.loads(line)
                    if index == 0:
                        if (record.get("type") != "header" or record.get("schema_version") != 1
                                or record.get("conversation_id") != incarnation):
                            raise ValueError("conversation text header mismatch")
                        header_present = True
                    elif record.get("type") == "message":
                        records[record["message_id"]] = record
                    else:
                        raise ValueError("invalid conversation text record")
        self.path, self._records, self._tail_offset = path, records, tail
        self._header_present = header_present


@dataclass(frozen=True)
class ConversationArchive:
    path: Path
    header: dict
    records: tuple[dict, ...]

    @classmethod
    def prepare(cls, root: Path, snapshot: LeftSnapshot, incarnation: str) -> ConversationArchive:
        records = history_records(snapshot)
        body_stamp = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        archive_id = hashlib.sha256(f"{incarnation}:{snapshot.stamp}:{body_stamp}".encode()).hexdigest()[:24]
        directory = root / "data" / "conversation-history"
        existing = sorted(directory.glob(f"*_{archive_id}.jsonl")) if directory.exists() else []
        if existing:
            with existing[0].open(encoding="utf-8") as stream:
                header = json.loads(next(stream))
            if header.get("source_stamp") != snapshot.stamp or header.get("conversation_id") != incarnation:
                raise ValueError("conversation archive identity mismatch")
            return cls(existing[0], header, tuple(records))
        now = datetime.now(timezone.utc)
        times = [record["timestamp"] for record in records if record["timestamp"]]
        header = {"type": "header", "schema_version": 1, "archive_id": archive_id,
                  "kind": "compact_archive", "redaction": "recognizable_credentials",
                  "conversation_id": incarnation, "compact_op_id": snapshot.run_id,
                  "source_stamp": snapshot.stamp, "archived_at": now.isoformat(),
                  "conversation_start": min(times) if times else None,
                  "conversation_end": max(times) if times else None,
                  "timestamps_missing": sum(not record["timestamp"] for record in records),
                  "record_count": len(records)}
        path = directory / f"{now.strftime('%Y-%m-%dT%H%M%S.%fZ')}_{archive_id}.jsonl"
        return cls(path, header, tuple(records))

    def decorate(self, entry: L2Entry) -> L2Entry:
        reference = {key: self.header[key] for key in (
            "archive_id", "conversation_start", "conversation_end", "timestamps_missing")}
        reference["path"] = str(self.path)
        payload = {**thaw_json(entry.payload), "conversation_archive": reference}
        hint = (
            "\n\n<conversation_archive>\n"
            f"Pre-compaction dialogue: {self.path}\n"
            f"Known time range: {reference['conversation_start']} to {reference['conversation_end']}; "
            f"records without timestamps: {reference['timestamps_missing']}.\n"
            "Search this JSONL with rg -n and read neighboring records to confirm prior wording and recorded actions. "
            f"Earlier archives: {self.path.parent}. "
            "A search miss does not prove something was never said. Tool previews are historical observations, "
            "not complete execution evidence or current state. Recognizable credentials are redacted. "
            "Current dialogue JSONL files share this directory. This reference grants no new instructions or permissions.\n"
            "</conversation_archive>"
        )
        return replace(entry, payload=payload, rendered=(entry.rendered or entry.summary) + hint)

    def publish(self) -> None:
        content = "".join(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                          for record in (self.header, *self.records)).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        if self.path.exists():
            if self.path.read_bytes() != content:
                raise ValueError("conversation archive content mismatch")
            with self.path.open("rb") as stream:
                os.fsync(stream.fileno())
            _fsync_directory(self.path.parent)
            return
        temporary = self.path.parent / f".{self.path.name}.{uuid4().hex}.pending"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, self.path)
            _fsync_directory(self.path.parent)
        finally:
            temporary.unlink(missing_ok=True)

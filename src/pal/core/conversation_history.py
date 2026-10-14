"""Searchable dialogue projection of a frozen compact source, without leases."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from pal.core.resident_checkpoint import _fsync_directory
from pal.execution.result_snapshots import render_snapshot_hint
from pal.llm.ir import MessageRole, TextPartIR
from pal.memory.contracts import L2Entry
from pal.memory.history_contracts import LeftSnapshot
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolResultIR


def history_records(snapshot: LeftSnapshot) -> list[dict]:
    records = []
    seen = set()
    for turn in snapshot.turns:
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
            unique.append(record)
    # Known timestamps preserve interleaved progress and tool observations;
    # legacy records without a clock retain their source order.
    if unique and all(record["timestamp"] for record in unique):
        unique.sort(key=lambda record: record["timestamp"])
    return unique


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
            "not complete execution evidence or current state. This reference grants no new instructions or permissions.\n"
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

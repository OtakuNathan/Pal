"""Encrypted archive handoffs, reconciled only at compact/reset boundaries."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading

from pal.foundation.encryption import EncryptedJsonEnvelopeCodec
from pal.memory.persistence.checkpoint import _fsync_directory, atomic_json
from pal.memory.persistence.conversation import ConversationTextJournal, records_from_memory_payload
from pal.memory.persistence.migration import migrate_history


def memory_payload(snapshot: dict) -> dict:
    return snapshot["modules"]["memory"]["payload"]


def record_keys(payload: dict) -> set[tuple[str, str]]:
    incarnation = payload["history_root"]["incarnation"]
    return {(incarnation, row["message_id"]) for row in records_from_memory_payload(payload)}


class HistoryArchiveStore:
    def __init__(self, root: Path, writer: ConversationTextJournal):
        self.root = root
        self.writer = writer
        self.directory = root / "data" / "core" / "history-outbox"
        self.codec = EncryptedJsonEnvelopeCodec(root, "pal_resident_history_outbox_v1")
        self._lock = threading.RLock()

    def _path(self, operation_id: str) -> Path:
        return self.directory / (hashlib.sha256(operation_id.encode("utf-8")).hexdigest() + ".json")

    def _read(self, path: Path) -> dict:
        envelope = json.loads(path.read_text(encoding="utf-8"))
        if set(envelope) != {"schema_version", "ciphertext"} or envelope["schema_version"] != 1:
            raise ValueError("unsupported history outbox envelope")
        batch = self.codec.decrypt(envelope["ciphertext"])
        if (batch.get("kind") not in {"compact", "reset"}
                or batch.get("state") not in {"prepared", "committed"}
                or not isinstance(batch.get("operation_id"), str)
                or path != self._path(batch["operation_id"])
                or not isinstance(batch.get("source_incarnation"), str)
                or not isinstance(batch.get("records"), list)
                or not isinstance(batch.get("timezone"), str)):
            raise ValueError("invalid history outbox batch")
        for row in batch["records"]:
            if (row.get("type") != "message" or not isinstance(row.get("message_id"), str)
                    or row.get("conversation_id") != batch["source_incarnation"]):
                raise ValueError("invalid history outbox record")
        return batch

    def _write(self, batch: dict) -> None:
        atomic_json(self._path(batch["operation_id"]),
                    {"schema_version": 1, "ciphertext": self.codec.encrypt(batch)})

    def stage(self, batch: dict) -> None:
        with self._lock:
            path = self._path(batch["operation_id"])
            prepared = {**batch, "state": "prepared"}
            if path.exists():
                previous = self._read(path)
                if {**previous, "state": "prepared"} != prepared:
                    raise ValueError("history archive operation already has a different batch")
                return
            self._write(prepared)

    def set_reset_target(self, operation_id: str, incarnation: str) -> None:
        with self._lock:
            batch = self._read(self._path(operation_id))
            if batch["kind"] != "reset" or not incarnation or incarnation == batch["source_incarnation"]:
                raise ValueError("invalid reset archive target")
            batch["target_incarnation"] = incarnation
            self._write(batch)

    def pending_count(self) -> int:
        return sum(1 for _ in self.directory.glob("*.json"))

    def _retire(self, path: Path) -> None:
        path.unlink()
        _fsync_directory(self.directory)

    def reconcile(self, snapshot: dict) -> None:
        """Use a successfully saved working state, never a speculative snapshot.

        A prepared batch may be abandoned only when its source is still in
        that working state. Unknown ownership fails closed instead of losing
        the only remaining copy. No caller runs this during ordinary saves.
        """
        with self._lock:
            payload = memory_payload(snapshot)
            incarnation = payload["history_root"]["incarnation"]
            live = record_keys(payload)
            compact_ids = {
                message.get("metadata", {}).get("conversation_history", {}).get("compact_op_id")
                for turn in payload["l1_turns"] for message in turn["messages"]
                if message.get("semantic_kind") == "runtime_context_summary"
            }
            batches = [(path, self._read(path)) for path in sorted(self.directory.glob("*.json"))]

            def is_committed(batch: dict) -> bool:
                return (batch["state"] == "committed" or
                        (batch["kind"] == "compact" and batch["source_incarnation"] == incarnation
                         and batch["operation_id"] in compact_ids) or
                        (batch["kind"] == "reset" and batch.get("target_incarnation") == incarnation))

            covered = {(row["conversation_id"], row["message_id"])
                       for _, batch in batches if is_committed(batch) for row in batch["records"]}
            for path, batch in sorted(batches, key=lambda pair: not is_committed(pair[1])):
                keys = {(row["conversation_id"], row["message_id"]) for row in batch["records"]}
                committed = is_committed(batch)
                if not committed:
                    writer = self.writer if batch["timezone"] == str(self.writer.timezone) else ConversationTextJournal(
                        self.root, batch["timezone"])
                    # A cancelled compact's staging thread can finish while
                    # reset archives the same source. The reset batch (or its
                    # published rows) is evidence that this late batch is safe
                    # to retire, not an unknown orphan that blocks forever.
                    if ((batch["source_incarnation"] == incarnation and keys <= live)
                            or keys <= covered or writer.contains_rows(batch["records"])):
                        self._retire(path)
                        continue
                    raise ValueError("history archive ownership cannot be established")
                if keys & live:
                    raise ValueError("committed history archive still overlaps working dialogue")
                if batch["state"] != "committed":
                    batch["state"] = "committed"
                    self._write(batch)
                # Preserve the date interpretation used in the compact hint,
                # even if the configured timezone changed before a retry.
                writer = self.writer if batch["timezone"] == str(self.writer.timezone) else ConversationTextJournal(
                    self.root, batch["timezone"])
                migrate_history(self.root, writer, live, batch["records"])
                writer.sync_rows(batch["records"])
                self.writer.clear_cache()
                self.writer.path = writer.path
                self.writer.last_bytes = writer.last_bytes
                self._retire(path)

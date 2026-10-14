"""Encrypted incremental runtime state over the existing resident checkpoint.

The disk cursor advances only after fsync. A new base is published before the
journal is retired, so either side of a process loss remains replayable.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
from typing import Any

from pal.core.resident_checkpoint import ResidentCheckpointError, ResidentCheckpointStore, _fsync_directory
from pal.core.runtime_state import validate_runtime_snapshot
from pal.foundation.encryption import EncryptedJsonEnvelopeCodec

JOURNAL_VERSION = 1
JOURNAL_KEY_PURPOSE = "pal_resident_journal_v1"


def _indexed(snapshot: dict) -> dict:
    value = deepcopy(snapshot)
    value.pop("sequence", None)
    memory = value["modules"].get("memory")
    if memory is not None:
        payload = memory["payload"]
        turns = payload.pop("l1_turns")
        payload["turn_order"] = [turn["turn_id"] for turn in turns]
        payload["turn_records"] = {}
        for turn in turns:
            messages = turn.pop("messages")
            turn["message_order"] = [message["message_id"] for message in messages]
            turn["message_records"] = {message["message_id"]: message for message in messages}
            payload["turn_records"][turn["turn_id"]] = turn
    return value


def _snapshot(indexed: dict, sequence: int) -> dict:
    value = deepcopy(indexed)
    value["sequence"] = sequence
    memory = value["modules"].get("memory")
    if memory is not None:
        payload = memory["payload"]
        records = payload.pop("turn_records")
        order = payload.pop("turn_order")
        if len(order) != len(set(order)) or set(order) != set(records):
            raise ResidentCheckpointError("journal turn order is invalid")
        turns = []
        for key in order:
            turn = records[key]
            messages = turn.pop("message_records")
            message_order = turn.pop("message_order")
            if len(message_order) != len(set(message_order)) or set(message_order) != set(messages):
                raise ResidentCheckpointError("journal message order is invalid")
            turn["messages"] = [messages[message_id] for message_id in message_order]
            turns.append(turn)
        payload["l1_turns"] = turns
    return validate_runtime_snapshot(value)


def _changes(old: Any, new: Any, path: tuple[str, ...] = ()) -> list[dict]:
    if old == new:
        return []
    if isinstance(old, dict) and isinstance(new, dict):
        result = [{"op": "delete", "path": [*path, key]} for key in sorted(old.keys() - new.keys())]
        for key in sorted(new):
            if key not in old:
                result.append({"op": "set", "path": [*path, key], "value": new[key]})
            else:
                result.extend(_changes(old[key], new[key], (*path, key)))
        return result
    if isinstance(old, list) and isinstance(new, list) and new[:len(old)] == old:
        return [{"op": "append", "path": list(path), "value": new[len(old):]}]
    return [{"op": "set", "path": list(path), "value": new}]


def _apply(value: dict, operations: list) -> None:
    for operation in operations:
        if not isinstance(operation, dict):
            raise ResidentCheckpointError("invalid journal operation")
        path = operation.get("path")
        kind = operation.get("op")
        fields = {"op", "path"} if kind == "delete" else {"op", "path", "value"}
        if (set(operation) != fields or kind not in {"set", "delete", "append"}
                or not isinstance(path, list) or not path or any(not isinstance(key, str) for key in path)):
            raise ResidentCheckpointError("invalid journal operation")
        parent = value
        for key in path[:-1]:
            parent = parent[key]
            if not isinstance(parent, dict):
                raise ResidentCheckpointError("invalid journal path")
        key = path[-1]
        if kind == "delete":
            del parent[key]
        elif kind == "append":
            if not isinstance(parent[key], list) or not isinstance(operation["value"], list):
                raise ResidentCheckpointError("invalid journal append")
            parent[key].extend(operation["value"])
        else:
            parent[key] = operation["value"]


class ResidentJournalStore:
    def __init__(self, runtime_root: Path):
        self.base = ResidentCheckpointStore(runtime_root)
        self.path = runtime_root / "data" / "core" / "resident_journal.jsonl"
        self.codec = EncryptedJsonEnvelopeCodec(runtime_root, JOURNAL_KEY_PURPOSE)
        self.sequence = 0
        self.state: dict | None = None
        self.tail_offset: int | None = None
        self.last_bytes = 0
        self._uncertain = False

    def read(self) -> dict | None:
        snapshot = self.base.read()
        if snapshot is None:
            if self.path.exists() and self.path.stat().st_size:
                raise ResidentCheckpointError("resident journal has no base checkpoint")
            self.state, self.sequence, self.tail_offset = None, 0, None
            return None
        state = _indexed(snapshot)
        sequence = int(snapshot["sequence"])
        offset = 0
        self.tail_offset = None
        if self.path.exists():
            with self.path.open("rb") as stream:
                for line in stream:
                    if not line.endswith(b"\n"):
                        self.tail_offset = offset
                        break
                    offset += len(line)
                    try:
                        envelope = json.loads(line)
                        if (set(envelope) != {"schema_version", "ciphertext"}
                                or type(envelope["schema_version"]) is not int
                                or envelope["schema_version"] != JOURNAL_VERSION
                                or not isinstance(envelope["ciphertext"], str)):
                            raise ValueError("unsupported journal envelope")
                        record = self.codec.decrypt(envelope["ciphertext"])
                        if set(record) != {"sequence", "previous_sequence", "runtime_spec_hash", "operations"}:
                            raise ValueError("invalid journal record")
                        next_sequence = record["sequence"]
                        if type(next_sequence) is not int or type(record["previous_sequence"]) is not int:
                            raise ValueError("invalid journal sequence")
                        if next_sequence < 1 or record["previous_sequence"] != next_sequence - 1:
                            raise ValueError("invalid journal predecessor")
                        if next_sequence <= int(snapshot["sequence"]):
                            continue  # A published base already covers this old journal.
                        if record["previous_sequence"] != sequence or next_sequence != sequence + 1:
                            raise ValueError("resident journal sequence gap")
                        if record["runtime_spec_hash"] != snapshot["runtime_spec_hash"]:
                            raise ValueError("resident journal runtime spec mismatch")
                        if not isinstance(record["operations"], list):
                            raise ValueError("invalid journal operations")
                        _apply(state, record["operations"])
                        sequence = next_sequence
                    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
                        raise ResidentCheckpointError("resident journal contains an invalid complete record") from exc
        restored = _snapshot(state, sequence)
        self.state, self.sequence = state, sequence
        return restored

    def save(self, snapshot: dict, *, rebase: bool = False) -> bool:
        if self._uncertain:
            # replace/fsync can fail after new bytes became visible. Re-read
            # the actual disk cursor and publish a fresh synchronized base;
            # otherwise a retry could reuse a covered sequence and lose R.
            self.read()
            rebase = True
        try:
            saved = self._save(snapshot, rebase=rebase)
            self._uncertain = False
            return saved
        except BaseException:
            self._uncertain = True
            raise

    def _save(self, snapshot: dict, *, rebase: bool) -> bool:
        snapshot = validate_runtime_snapshot(snapshot)
        state = _indexed(snapshot)
        operations = _changes(self.state, state) if self.state is not None else []
        if not rebase and self.state is not None and not operations:
            self.last_bytes = 0
            return False
        sequence = self.sequence + 1
        snapshot = {**snapshot, "sequence": sequence}
        if rebase or self.state is None:
            self.base.publish(snapshot)
            # Publish updates our cursor even if retiring the superseded log
            # fails. Its covered records remain valid on the next restore.
            self.state, self.sequence = state, sequence
            self.last_bytes = self.base.path.stat().st_size
            self._retire_journal()
            return True
        record = {
            "sequence": sequence, "previous_sequence": self.sequence,
            "runtime_spec_hash": snapshot["runtime_spec_hash"], "operations": operations,
        }
        line = (json.dumps({"schema_version": JOURNAL_VERSION, "ciphertext": self.codec.encrypt(record)},
                           separators=(",", ":")) + "\n").encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        created = not self.path.exists()
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT, 0o600)
        previous_size = os.fstat(descriptor).st_size
        try:
            if self.tail_offset is not None:
                os.ftruncate(descriptor, self.tail_offset)
                previous_size = self.tail_offset
            os.lseek(descriptor, 0, os.SEEK_END)
            remaining = memoryview(line)
            while remaining:
                count = os.write(descriptor, remaining)
                if count <= 0:
                    raise OSError("resident journal write made no progress")
                remaining = remaining[count:]
            os.fsync(descriptor)
            if created:
                _fsync_directory(self.path.parent)
        except BaseException:
            # A retry must not append after a failed write. Keep the last
            # acknowledged cursor even when fsync itself fails.
            os.ftruncate(descriptor, previous_size)
            raise
        finally:
            os.close(descriptor)
        self.tail_offset = None
        self.state, self.sequence = state, sequence
        self.last_bytes = len(line)
        return True

    def _retire_journal(self) -> None:
        self.path.unlink(missing_ok=True)
        _fsync_directory(self.path.parent)
        self.tail_offset = None

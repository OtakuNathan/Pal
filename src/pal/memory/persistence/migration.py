"""One-time, resumable history normalization at an archive boundary."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from uuid import uuid4

from pal.memory.persistence.checkpoint import _fsync_directory, atomic_json
from pal.memory.persistence.conversation import ConversationTextJournal, redact_conversation_text


def _legacy_rows(directory: Path) -> dict[tuple[str, str], dict]:
    selected = {}
    minimum = datetime.min.replace(tzinfo=timezone.utc)
    for path in sorted(directory.glob("*.jsonl")):
        header = None
        with path.open("rb") as stream:
            for index, line in enumerate(stream):
                if not line.endswith(b"\n"):
                    break  # Keep the original uncertain tail in the backup.
                record = json.loads(line)
                if index == 0:
                    header = record
                    if (header.get("type") != "header" or
                            (header.get("schema_version"), header.get("kind")) not in {
                                (1, "compact_archive"), (1, "current_dialogue"), (2, "daily_dialogue")}):
                        raise ValueError("unsupported legacy history header")
                    continue
                if record.get("type") != "message" or not header:
                    raise ValueError("invalid legacy history record")
                incarnation = record.get("conversation_id") or header.get("conversation_id")
                if not incarnation or not record.get("message_id"):
                    raise ValueError("legacy history record has no stable identity")
                recorded_at = header.get("archived_at") or header.get("recorded_at")
                moment = datetime.fromisoformat(recorded_at) if recorded_at else minimum
                if moment.tzinfo is None:
                    moment = minimum
                priority = (header["schema_version"], moment, path.name, index)
                key = (incarnation, record["message_id"])
                if key not in selected or priority > selected[key][0]:
                    text = redact_conversation_text(record["text"])
                    selected[key] = (priority, {**record, "conversation_id": incarnation,
                                               "text": text, "redacted": bool(record.get("redacted")) or text != record["text"]})
    return {key: row for key, (_, row) in selected.items()}


def migrate_history(root: Path, writer: ConversationTextJournal,
                    live: set[tuple[str, str]], incoming: list[dict]) -> None:
    marker = root / "data" / "core" / "history-layout.json"
    state = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None
    if state is not None:
        if state.get("schema_version") != 1 or state.get("phase") not in {"prepared", "done"}:
            raise ValueError("invalid history migration marker")
        if state["phase"] == "done":
            return
    else:
        migration_id = uuid4().hex
        stage = root / "data" / (".history-migration-" + migration_id)
        rows = _legacy_rows(writer.directory)
        rows = {key: row for key, row in rows.items() if key not in live}
        rows.update(((row["conversation_id"], row["message_id"]), row) for row in incoming)
        target = ConversationTextJournal(root, str(writer.timezone), directory=stage)
        target.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.sync_rows(rows.values())
        _fsync_directory(stage)
        _fsync_directory(stage.parent)
        state = {"schema_version": 1, "phase": "prepared", "migration_id": migration_id}
        atomic_json(marker, state)

    migration_id = state.get("migration_id", "")
    if len(migration_id) != 32 or any(c not in "0123456789abcdef" for c in migration_id):
        raise ValueError("invalid history migration identity")
    stage = root / "data" / (".history-migration-" + migration_id)
    backup = root / "data" / "history-backups" / migration_id
    original = writer.directory
    if stage.exists():
        if original.exists():
            if backup.exists():
                raise ValueError("ambiguous history directory migration")
            backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.replace(original, backup)
            _fsync_directory(backup.parent)
            _fsync_directory(original.parent)
        os.replace(stage, original)
        _fsync_directory(original.parent)
    elif not original.is_dir():
        raise ValueError("history migration has no recoverable directory")
    atomic_json(marker, {**state, "phase": "done"})
    writer.clear_cache()

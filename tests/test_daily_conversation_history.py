"""Daily history partitions preserve evidence across commit and restart boundaries."""
import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from pal.core import PalCore as _PalCoreBootstrap
from pal.memory.persistence.conversation import ConversationTextJournal


def message(identity, timestamp, text="text"):
    return {"type": "message", "message_id": identity, "turn_id": "turn",
            "role": "user", "timestamp": timestamp, "text": text}


def messages(path):
    return [row for line in path.read_text().splitlines()
            if (row := json.loads(line))["type"] == "message"]


@pytest.mark.parametrize("zone,stamp,day", [
    ("UTC", "2000-01-01T23:59:59+00:00", "2000-01-01"),
    ("Asia/Shanghai", "2000-01-01T23:59:59+00:00", "2000-01-02"),
    ("Europe/London", "2026-07-01T23:30:00+00:00", "2026-07-02"),
    ("Europe/London", "2026-01-01T23:30:00+00:00", "2026-01-01"),
    ("America/New_York", "2026-07-02T00:30:00+00:00", "2026-07-01"),
])
def test_partition_uses_occurrence_date_in_configured_timezone(tmp_path, zone, stamp, day):
    writer = ConversationTextJournal(tmp_path, zone)
    writer.sync([message("id", stamp)], "session")
    path = writer.directory / f"{day}.jsonl"
    header = json.loads(path.read_text().splitlines()[0])
    assert header["date"] == day and header["timezone"] == zone
    assert messages(path)[0]["timestamp"] == stamp
    assert messages(path)[0]["conversation_id"] == "session"


def test_one_delayed_batch_spans_midnight_and_restart_does_not_duplicate(tmp_path):
    writer = ConversationTextJournal(tmp_path)
    rows = [message("before", "2000-01-01T23:59:59+00:00", "before midnight"),
            message("after", "2000-01-02T00:00:01+00:00", "after midnight")]
    writer.sync(rows, "session")
    paths = sorted(writer.directory.glob("*.jsonl"))
    assert [path.name for path in paths] == ["2000-01-01.jsonl", "2000-01-02.jsonl"]
    before = {path: path.read_bytes() for path in paths}
    restarted = ConversationTextJournal(tmp_path)
    restarted.sync(rows, "session")
    assert restarted.last_bytes == 0
    assert {path: path.read_bytes() for path in paths} == before


@pytest.mark.parametrize("stamp", [None, "", "2000-01-01T12:00:00", "unknown"])
def test_legacy_unknown_clock_is_not_assigned_todays_date(tmp_path, stamp):
    writer = ConversationTextJournal(tmp_path)
    writer.sync([message("old", stamp)], "session")
    assert [path.name for path in writer.directory.glob("*.jsonl")] == ["undated.jsonl"]
    assert messages(writer.path)[0]["timestamp"] == stamp


def test_shared_day_distinguishes_incarnations_and_appends_corrections(tmp_path):
    stamp = "2000-01-01T12:00:00+00:00"
    writer = ConversationTextJournal(tmp_path)
    writer.sync([message("id", stamp, "old session")], "first")
    prefix = writer.path.read_bytes()
    writer.sync([message("id", stamp, "new session")], "second")
    writer.sync([message("id", stamp, "corrected new session")], "second")
    assert writer.path.read_bytes().startswith(prefix)
    rows = messages(writer.path)
    assert [(row["conversation_id"], row["text"]) for row in rows] == [
        ("first", "old session"), ("second", "new session"), ("second", "corrected new session")]
    restarted = ConversationTextJournal(tmp_path)
    restarted.sync([message("id", stamp, "corrected new session")], "second")
    assert restarted.last_bytes == 0
    assert messages(restarted.path) == rows


def test_partial_multiday_write_retries_only_missing_partition(tmp_path):
    writer = ConversationTextJournal(tmp_path)
    rows = [message("first", "2000-01-01T12:00:00+00:00"),
            message("second", "2000-01-02T12:00:00+00:00")]
    append = writer._append

    def fail_second(path, batch):
        if path.stem == "2000-01-02":
            raise OSError("disk unavailable")
        append(path, batch)

    with patch.object(writer, "_append", side_effect=fail_second), pytest.raises(OSError):
        writer.sync(rows, "session")
    first = writer.directory / "2000-01-01.jsonl"
    saved = first.read_bytes()
    ConversationTextJournal(tmp_path).sync(rows, "session")
    assert first.read_bytes() == saved
    assert len(messages(writer.directory / "2000-01-02.jsonl")) == 1


def test_corrupt_complete_line_is_preserved_and_not_silently_overwritten(tmp_path):
    writer = ConversationTextJournal(tmp_path)
    row = message("first", "2000-01-01T12:00:00+00:00")
    writer.sync([row], "session")
    with writer.path.open("ab") as stream:
        stream.write(b'{"broken":}\n')
    before = writer.path.read_bytes()
    with pytest.raises(ValueError):
        ConversationTextJournal(tmp_path).sync([row], "session")
    assert writer.path.read_bytes() == before


def test_shared_archive_writer_serializes_duplicate_batches(tmp_path):
    writer = ConversationTextJournal(tmp_path)
    rows = [message("first", "2000-01-01T12:00:00+00:00")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: writer.sync(rows, "session"), range(8)))
    assert len(messages(writer.path)) == 1


def test_low_level_writer_leaves_legacy_files_for_boundary_migration(tmp_path):
    writer = ConversationTextJournal(tmp_path)
    writer.directory.mkdir(parents=True)
    old_current = writer.directory / "current_legacy.jsonl"
    old_archive = writer.directory / "2025-01-01T000000.000000Z_legacy.jsonl"
    old_current.write_text('{"type":"message","text":"old current dialogue"}\n')
    old_archive.write_text('{"type":"message","text":"old compact dialogue"}\n')
    before = {path: path.read_bytes() for path in (old_current, old_archive)}
    writer.sync([message("new", "2000-01-01T12:00:00+00:00")], "session")
    assert {path: path.read_bytes() for path in before} == before
    assert len(list(writer.directory.glob("*.jsonl"))) == 3

"""Verify that model-visible paths and text identify the effects requested."""
from __future__ import annotations

import re

import pytest

from tests.test_tool_failure_affordances import invoke, metadata, read, runtime


@pytest.mark.parametrize("kind", ["file", "directory", "dangling", "self", "root"])
@pytest.mark.parametrize("recursive", [False, True])
def test_delete_unlinks_final_symlink_and_preserves_target(runtime, tmp_path, kind, recursive):
    target = tmp_path / "target"
    link = tmp_path / "link"
    if kind == "file":
        target.write_text("target must survive")
    elif kind == "directory":
        target.mkdir()
        (target / "sentinel").write_text("target must survive")
    elif kind == "self":
        target = link
    elif kind == "root":
        target = tmp_path.anchor
    link.symlink_to(target, target_is_directory=kind in {"directory", "root"})

    result = invoke(runtime, "delete_path", {"file_path": str(link), "recursive": recursive})

    assert result.ok, result.llm_text
    assert not link.is_symlink()
    assert result.structured["file_path"] == str(link)
    assert result.structured["path_kind"] == "symlink"
    assert result.structured["recursive"] is False
    assert metadata(result)["effect"] == "applied"
    if kind == "file":
        assert target.read_text() == "target must survive"
    elif kind == "directory":
        assert (target / "sentinel").read_text() == "target must survive"


def test_delete_resolves_parent_symlinks_but_unlinks_final_entry(runtime, tmp_path):
    directory = tmp_path / "real"
    directory.mkdir()
    parent_link = tmp_path / "parent"
    parent_link.symlink_to(directory, target_is_directory=True)
    target = tmp_path / "target"
    target.write_text("target must survive")
    link = directory / "child"
    link.symlink_to(target)

    result = invoke(runtime, "delete_path", {"file_path": str(parent_link / "child")})

    assert result.ok, result.llm_text
    assert not link.is_symlink()
    assert parent_link.is_symlink()
    assert target.read_text() == "target must survive"


def test_symlink_digest_is_rejected_without_reading_or_deleting_target(runtime, tmp_path):
    target = tmp_path / "target"
    target.write_text("target must survive")
    link = tmp_path / "link"
    link.symlink_to(target)

    result = invoke(runtime, "delete_path", {"file_path": str(link), "expected_sha256": "0" * 64})

    assert not result.ok
    info = metadata(result)
    assert (info["kind"], info["effect"], info["error_code"]) == (
        "rejected", "not_started", "SHA256_NOT_SUPPORTED_FOR_SYMLINK")
    assert "link itself" in result.llm_text
    assert link.is_symlink() and target.read_text() == "target must survive"


@pytest.mark.parametrize("original", [
    b"alpha\r\nbeta\r\ngamma\r\n",
    b"alpha\r\nbeta\ngamma\r\n",
    b"alpha\rbeta\rgamma\r",
])
@pytest.mark.parametrize("partial", [False, True])
def test_displayed_multiline_round_trips_exact_newlines(runtime, tmp_path, original, partial):
    path = tmp_path / "source"
    path.write_bytes(original)
    reading = read(runtime, path, **({"ranges": [{"offset": 1, "limit": 2}]} if partial else {}))
    # Remove only display labels. Keep the exact line endings delivered to the model.
    copied = "".join(re.sub(r"^ *\d+\t", "", line)
                     for line in reading.llm_text.splitlines(keepends=True)
                     if re.match(r"^ *\d+\t", line)).removesuffix("\n")
    assert copied in original.decode()
    assert "encode CRLF as \\r\\n" in reading.llm_text
    replacement = copied.replace("beta", "BETA")

    result = invoke(runtime, "edit_file", {"file_path": str(path), "edits": [
        {"old_string": copied, "new_string": replacement}]})

    assert result.ok, result.llm_text
    assert path.read_bytes() == original.replace(b"beta", b"BETA")


def test_crlf_match_failure_explains_the_required_json_line_endings(runtime, tmp_path):
    path = tmp_path / "source"
    path.write_bytes(b"alpha\r\nbeta\r\n")
    read(runtime, path)

    result = invoke(runtime, "edit_file", {"file_path": str(path), "edits": [
        {"old_string": "alpha\nbeta", "new_string": "changed"}]})

    assert not result.ok
    assert metadata(result)["error_code"] == "NOT_FOUND_MATCH"
    assert "encode CRLF as \\r\\n" in metadata(result)["recovery"]
    assert path.read_bytes() == b"alpha\r\nbeta\r\n"


def test_external_snapshot_link_can_be_unlinked_but_snapshot_entries_are_protected(runtime, tmp_path):
    snapshots = runtime.result_snapshots
    ref = snapshots.capture("snapshot content", call_id="audit", lifetime="audit")
    link = tmp_path / "snapshot-link"
    link.symlink_to(ref.path)
    result = invoke(runtime, "delete_path", {"file_path": str(link)})
    assert result.ok, result.llm_text
    assert not link.is_symlink()
    assert snapshots.lookup_path(ref.path) == ref

    protected = snapshots.root / "protected-link"
    protected.symlink_to(tmp_path / "absent")
    result = invoke(runtime, "delete_path", {"file_path": str(protected)})
    assert not result.ok
    assert metadata(result)["error_code"] == "immutable_result_snapshot"
    assert protected.is_symlink()

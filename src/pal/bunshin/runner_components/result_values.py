from __future__ import annotations
import json
import os
from dataclasses import field
from pathlib import Path
from typing import Any
from pal.plugins.l3 import MockL3Plugin


def _memory_candidates_from_sink(memory_candidate_sink: MockL3Plugin) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for record in list(getattr(memory_candidate_sink, "records", []) or []):
        if not isinstance(record, dict):
            continue
        item = {
            "document_id": str(record.get("document_id") or ""),
            "kind": str(record.get("document_kind") or record.get("kind") or ""),
            "scope": str(record.get("scope") or "task"),
            "title": str(record.get("title") or ""),
            "summary": str(record.get("summary") or ""),
            "search_text": str(record.get("search_text") or ""),
            "canonical_key": str(record.get("canonical_key")) if record.get("canonical_key") is not None else None,
            "dedupe_fingerprint": str(record.get("dedupe_fingerprint")) if record.get("dedupe_fingerprint") is not None else None,
            "topics": list(record.get("topics") or []),
            "payload": dict(record.get("payload") or {}),
            "source_excerpt": str(record.get("source_excerpt") or record.get("search_text") or ""),
            "why_durable": str(record.get("why_durable") or ""),
            "source_kind": "bunshin_candidate_sink",
            "candidate_state": "candidate",
        }
        payload = item["payload"]
        if item["kind"] == "case" and all(str(payload.get(field) or "").strip() for field in ("situation", "task", "action", "result")):
            item["star"] = {field: str(payload.get(field) or "") for field in ("situation", "task", "action", "result")}
        if item["summary"].strip() or item["title"].strip():
            result.append(item)
    return result


def _extract_ask_user_question_payload(text: str) -> dict[str, Any]:
    loaded = _try_extract_json(str(text or "").strip())
    if not isinstance(loaded, dict):
        return {}
    output_type = str(loaded.get("type") or loaded.get("output_type") or "").strip().lower()
    if output_type not in {"ask_user_question", "clarification_request"} and "questions" not in loaded:
        return {}
    questions = [dict(item) for item in list(loaded.get("questions") or []) if isinstance(item, dict)]
    if not questions:
        return {}
    payload = {
        "type": "ask_user_question",
        "task_id": str(loaded.get("task_id") or ""),
        "workflow_id": str(loaded.get("workflow_id") or ""),
        "invocation_id": str(loaded.get("invocation_id") or ""),
        "questions": questions[:3],
    }
    return payload


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _extract_lessons_and_clean_summary(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    lessons = {"task_lessons": [], "system_lessons": []}
    if not raw:
        return {"summary": "", **lessons}
    loaded = _try_extract_json(raw)
    if isinstance(loaded, dict):
        lessons["task_lessons"].extend(_string_items(loaded.get("task_lessons") or loaded.get("taskLessons") or loaded.get("task_lessons_to_remember")))
        lessons["system_lessons"].extend(_string_items(loaded.get("system_lessons") or loaded.get("systemLessons") or loaded.get("system_lesson_candidates")))
        summary = str(loaded.get("summary") or loaded.get("final_summary") or loaded.get("result") or raw).strip()
        return {"summary": summary, **{key: _dedupe_nonempty(value) for key, value in lessons.items()}}

    current: str | None = None
    summary_lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip().strip("-* ")
        heading = _lesson_heading_kind(stripped)
        if heading == "task_lessons":
            current = "task_lessons"
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif heading == "system_lessons":
            current = "system_lessons"
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif current and stripped:
            value = stripped
        else:
            current = None
            value = ""
            summary_lines.append(line.rstrip())
        if current and value and value.lower() not in {"none", "n/a"}:
            lessons[current].append(value)
    summary_text = "\n".join(summary_lines).strip()
    return {"summary": summary_text, **{key: _dedupe_nonempty(value) for key, value in lessons.items()}}


def _compact_preview_text(value: str) -> str:
    lines: list[str] = []
    blank_pending = False
    for raw_line in str(value or "").strip().splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            blank_pending = bool(lines)
            continue
        if blank_pending:
            lines.append("")
            blank_pending = False
        lines.append(line)
    return "\n".join(lines).strip()


def _strip_single_json_code_fence(value: str) -> str:
    raw = str(value or "").strip()
    if not raw.startswith("```"):
        return raw
    lines = raw.splitlines()
    if not lines or not lines[0].strip().startswith("```"):
        return raw
    opener = lines[0].strip().lower()
    if opener not in {"```", "```json", "```jsonc"}:
        return raw
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return raw
    return "\n".join(lines[1:-1]).strip()


def _lesson_heading_kind(text: str) -> str:
    normalized = str(text or "").strip().strip("#*_` ")
    while normalized and not (normalized[0].isalnum() or normalized[0] == "_"):
        normalized = normalized[1:].strip()
    lowered = normalized.lower().replace("_", " ")
    lowered = lowered.rstrip(":").strip()
    if lowered in {"task lesson", "task lessons", "task wise lessons", "task-wise lessons"}:
        return "task_lessons"
    if lowered in {"system lesson", "system lessons", "system wise lessons", "system-wise lessons"}:
        return "system_lessons"
    if lowered.startswith(("task lesson:", "task lessons:", "task wise lessons:", "task-wise lessons:")):
        return "task_lessons"
    if lowered.startswith(("system lesson:", "system lessons:", "system wise lessons:", "system-wise lessons:")):
        return "system_lessons"
    return ""


def _extract_lessons(text: str) -> dict[str, list[str]]:
    payload = _extract_lessons_and_clean_summary(text)
    return {
        "task_lessons": list(payload.get("task_lessons") or []),
        "system_lessons": list(payload.get("system_lessons") or []),
    }
    raw = str(text or "").strip()
    lessons = {"task_lessons": [], "system_lessons": []}
    if not raw:
        return lessons
    loaded = _try_extract_json(raw)
    if isinstance(loaded, dict):
        lessons["task_lessons"].extend(_string_items(loaded.get("task_lessons") or loaded.get("taskLessons") or loaded.get("task_lessons_to_remember")))
        lessons["system_lessons"].extend(_string_items(loaded.get("system_lessons") or loaded.get("systemLessons") or loaded.get("system_lesson_candidates")))
    current: str | None = None
    for line in raw.splitlines():
        stripped = line.strip().strip("-* ")
        lowered = stripped.lower()
        if lowered.startswith(("task lesson:", "task lessons:", "task_lessons:", "task-wise lessons:", "task wise lessons:")):
            current = "task_lessons"
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif lowered.startswith(("system lesson:", "system lessons:", "system_lessons:", "system-wise lessons:", "system wise lessons:")):
            current = "system_lessons"
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif current and stripped:
            value = stripped
        else:
            value = ""
        if current and value and value.lower() not in {"none", "n/a", "无", "没有"}:
            lessons[current].append(value)
    return {key: _dedupe_nonempty(value) for key, value in lessons.items()}


def _try_extract_json(text: str) -> Any:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except Exception:
        return None


def _string_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _dedupe_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = " ".join(str(value or "").split())
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

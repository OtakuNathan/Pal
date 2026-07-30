from __future__ import annotations

import re
from typing import Any

from pal.memory.candidates import memory_star_from_args
from pal.memory.contracts import L1MessageKind, L1TranscriptMessage, L2Entry

SUMMARY_ENTRY_ID = "memory_summary_current"
SUMMARY_TITLE = "Conversation Summary"
_PERSISTENT_SYSTEM_REMINDER_RE = re.compile(
    r"\s*<system-reminder\b[^>]*>.*?</system-reminder>\s*",
    re.IGNORECASE | re.DOTALL,
)

def normalize_l1_message_kind(
    value: object,
    *,
    role: str,
    tool_calls: object = None,
    tool_call_id: object = None,
) -> L1MessageKind:
    raw = str(value or "").strip()
    if raw:
        try:
            return L1MessageKind(raw)
        except ValueError:
            pass
    normalized_role = str(role or "").strip()
    if normalized_role == "tool":
        return L1MessageKind.TOOL_RESULT
    if normalized_role == "assistant" and tool_calls:
        return L1MessageKind.ASSISTANT_TOOL_CALL
    if normalized_role == "assistant":
        return L1MessageKind.ASSISTANT_REPLY
    if normalized_role == "user":
        return L1MessageKind.USER_REQUEST
    if tool_call_id:
        return L1MessageKind.TOOL_RESULT
    return L1MessageKind.ASSISTANT_REPLY


def normalize_l1_transcript(item: list[L1TranscriptMessage] | list[dict[str, object]] | str) -> list[L1TranscriptMessage]:
    if isinstance(item, str):
        content = item.strip()
        return [L1TranscriptMessage(role="assistant", content=content)] if content else []
    normalized: list[L1TranscriptMessage] = []
    for entry in list(item or []):
        if isinstance(entry, L1TranscriptMessage):
            role = str(entry.role or "").strip()
            content = entry.content.strip()
            tool_calls = entry.tool_calls
            tool_call_id = entry.tool_call_id
            if content or (role == "assistant" and tool_calls) or (role == "tool" and tool_call_id):
                normalized.append(
                    L1TranscriptMessage(
                        role=role,
                        content=content,
                        kind=normalize_l1_message_kind(
                            entry.kind,
                            role=role,
                            tool_calls=tool_calls,
                            tool_call_id=tool_call_id,
                        ),
                        tool_calls=tool_calls,
                        tool_call_id=tool_call_id,
                        payload=dict(entry.payload or {}),
                    )
                )
            continue
        if isinstance(entry, dict):
            role = str(entry.get("role") or "").strip()
            content = str(entry.get("content") or "").strip()
            tool_calls = entry.get("tool_calls")
            tool_call_id = entry.get("tool_call_id")
            if role and (content or (role == "assistant" and tool_calls) or (role == "tool" and tool_call_id)):
                normalized.append(
                    L1TranscriptMessage(
                        role=role,
                        content=content,
                        kind=normalize_l1_message_kind(
                            entry.get("kind"),
                            role=role,
                            tool_calls=tool_calls,
                            tool_call_id=tool_call_id,
                        ),
                        tool_calls=tool_calls,
                        tool_call_id=tool_call_id,
                        payload=dict(entry.get("payload") or {}),
                    )
                )
    return normalized


def flatten_l1_context(items: list[list[L1TranscriptMessage]]) -> list[L1TranscriptMessage]:
    flattened: list[L1TranscriptMessage] = []
    for transcript in items:
        flattened.extend(normalize_l1_transcript(transcript))
    return flattened


def strip_persistent_system_reminders(text: str) -> str:
    return _PERSISTENT_SYSTEM_REMINDER_RE.sub("\n\n", str(text or "")).strip()


def current_summary_from_l1(items: list[list[L1TranscriptMessage]]) -> L2Entry | None:
    for message in flatten_l1_context(items):
        kind = normalize_l1_message_kind(
            message.kind,
            role=str(message.role or "").strip(),
            tool_calls=message.tool_calls,
            tool_call_id=message.tool_call_id,
        )
        if kind != L1MessageKind.RUNTIME_CONTEXT_SUMMARY:
            continue
        content = str(message.content or "").strip()
        if not content:
            continue
        payload = dict(message.payload or {})
        summary_payload = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        summary_text = str(summary_payload.get("summary") or "").strip() if isinstance(summary_payload, dict) else ""
        search_text = str(summary_payload.get("search_text") or "").strip() if isinstance(summary_payload, dict) else ""
        return L2Entry(
            entry_id=SUMMARY_ENTRY_ID,
            kind="summary",
            scope="system",
            title=SUMMARY_TITLE,
            summary=summary_text or content,
            source_kind="l1_compaction",
            candidate_state="stable",
            rendered=content,
            search_text=search_text or summary_text or content,
            payload=payload,
        )
    return None


def memory_candidates_from_compact_result(result: Any) -> list[dict[str, Any]]:
    if result is None:
        return []
    for entry in list(getattr(result, "projected_entries", []) or []):
        payload = getattr(entry, "payload", None)
        if isinstance(payload, dict):
            candidates = coerce_memory_candidate_list(payload.get("memory_candidates"))
            if candidates:
                return candidates
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        return coerce_memory_candidate_list(metadata.get("memory_candidates"))
    return []


def coerce_memory_candidate_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        candidate = dict(item)
        kind = str(candidate.get("kind") or "case").strip() or "case"
        candidate["kind"] = kind
        star, star_error = memory_star_from_args(candidate)
        if kind == "case":
            if star_error or not star:
                continue
            candidate["star"] = star
        elif star:
            continue
        result.append(candidate)
    return result

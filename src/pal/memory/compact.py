from __future__ import annotations

import re
from typing import Any

from pal.memory.candidates import memory_star_from_args
from pal.memory.contracts import L1MessageKind, L1TranscriptMessage

SUMMARY_ENTRY_ID = "memory_summary_current"
SUMMARY_TITLE = "Conversation Summary"
_TRANSIENT_PROVIDER_PAYLOAD_KEYS = frozenset(
    {
        "provider_specific_fields",
        "reasoning_content",
        "reasoning_text",
        "anthropic_thinking_blocks",
    }
)
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
                        payload=_durable_l1_payload(entry.payload),
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
                        payload=_durable_l1_payload(entry.get("payload")),
                    )
                )
    return normalized


def _durable_l1_payload(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if str(key) not in _TRANSIENT_PROVIDER_PAYLOAD_KEYS
    }


def flatten_l1_context(items: list[list[L1TranscriptMessage]]) -> list[L1TranscriptMessage]:
    flattened: list[L1TranscriptMessage] = []
    for transcript in items:
        flattened.extend(normalize_l1_transcript(transcript))
    return flattened


def strip_persistent_system_reminders(text: str) -> str:
    return _PERSISTENT_SYSTEM_REMINDER_RE.sub("\n\n", str(text or "")).strip()


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


def compact_normalization_diagnostics(result: Any) -> list[str]:
    diagnostics = []
    for entry in list(getattr(result, "projected_entries", []) or []):
        payload = getattr(entry, "payload", {})
        if isinstance(payload, dict):
            diagnostics.extend(item for item in payload.get("compaction_diagnostics", []) if isinstance(item, str))
    return diagnostics


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

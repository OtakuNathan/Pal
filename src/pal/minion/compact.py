from __future__ import annotations

import json
import re
from typing import Any

from pal.foundation import utc_now
from pal.memory.compact import SUMMARY_ENTRY_ID, SUMMARY_TITLE, normalize_l1_message_kind, normalize_l1_transcript
from pal.memory.contracts import L1MessageKind, L1TranscriptMessage, L2Entry, MemoryCompactRequest, MemoryCompactResult

COMPACTION_SCHEMA_MINION_V1 = "pal.compaction.minion.v1"

MINION_COMPACT_MIN_PRIOR_USER_INPUT_BUDGET = 4096

_MINION_MANAGEMENT_LINE_RE = re.compile(
    r"""^\s*(?:[-*]\s*)?(?:["']?)"""
    r"(?:work_order_id|run_id|minion_id|ledger_id|checkpoint_id|correlation_id|task_id|module_id)"
    r"""(?:["']?)\s*[:=]""",
    re.IGNORECASE,
)
_MINION_MANAGEMENT_KEYS = frozenset({
    "work_order_id",
    "run_id",
    "minion_id",
    "ledger_id",
    "checkpoint_id",
    "correlation_id",
    "task_id",
    "module_id",
})


def build_minion_prior_user_inputs_source_text(
    items: list[list[L1TranscriptMessage]],
    *,
    target_input_budget: int,
) -> str:
    user_inputs, dropped_count = _bounded_minion_prior_user_inputs(
        collect_minion_prior_user_inputs(items),
        target_input_budget=target_input_budget,
    )
    if not user_inputs and dropped_count <= 0:
        return ""
    lines = [
        '<compact_source kind="minion" mode="prior_completed_user_inputs">',
        "## Source Rules",
        "- This source is mechanically assembled from committed minion turns before the current active turn.",
        "- It contains only prior user/task inputs. Assistant replies, tool results, checklist state, ledger state, checkpoint state, and current turn state are excluded.",
        "- Treat these prior inputs as already handled or superseded unless current source-of-truth artifacts say otherwise.",
        "- Continue from the current active milestone turn; current user message and current tool protocol are not compacted here.",
        "",
        "## Prior Completed User Inputs",
    ]
    if dropped_count > 0:
        lines.append(f"Older prior user inputs omitted for budget: {dropped_count}.")
        lines.append("")
    if user_inputs:
        for index, text in enumerate(user_inputs, start=1):
            lines.extend([f"### Prior Input {index}", text])
    else:
        lines.append("No prior completed user inputs retained.")
    lines.append("</compact_source>")
    return "\n\n".join(str(line or "").strip() for line in lines if str(line or "").strip()).strip()


def build_minion_prior_user_inputs_compaction_payload(
    items: list[list[L1TranscriptMessage]],
    *,
    target_input_budget: int,
) -> dict[str, Any]:
    user_inputs, dropped_count = _bounded_minion_prior_user_inputs(
        collect_minion_prior_user_inputs(items),
        target_input_budget=target_input_budget,
    )
    has_inputs = bool(user_inputs)
    summary_text = (
        "Minion run memory was compacted mechanically: prior user inputs are retained as already-handled history. "
        "Continue from the current active milestone turn."
        if has_inputs
        else "Minion run memory was compacted mechanically: no prior user inputs were retained. "
        "Continue from the current active milestone turn."
    )
    continuity: dict[str, Any] = {
        "history_rule": (
            "Prior user inputs are background only: treat them as already handled or superseded unless checklist, "
            "ledger, checkpoint, workspace, or the current turn says otherwise."
        ),
        "current_turn_rule": (
            "The current active milestone user message and tool protocol are authoritative and are intentionally "
            "not compacted."
        ),
    }
    if has_inputs:
        continuity["prior_completed_user_inputs"] = user_inputs
    if dropped_count > 0:
        continuity["retired_prior_user_input_count"] = dropped_count
    search_text = "\n\n".join(user_inputs).strip()
    return {
        "schema": COMPACTION_SCHEMA_MINION_V1,
        "kind": "minion",
        "continuity": continuity,
        "summary": {
            "summary": summary_text,
            "search_text": search_text or summary_text,
        },
    }


def compact_minion_memory_service(memory_service: Any, request: MemoryCompactRequest) -> MemoryCompactResult:
    if not request.metadata.get("structured_compaction") and not str(request.metadata.get("semantic_summary") or "").strip():
        raise ValueError("minion compact requires structured_compaction or semantic_summary")
    summary_entry = coerce_minion_compaction_summary_entry(
        request.metadata.get("structured_compaction"),
        fallback_summary=str(request.metadata.get("semantic_summary") or "").strip(),
    )
    l1_store = getattr(memory_service, "l1_store", None)
    if l1_store is None or not hasattr(l1_store, "items"):
        raise ValueError("minion compact requires an underlying memory service with l1_store.items")
    l2_store = getattr(memory_service, "l2_store", None)
    previous_l1_items = list(getattr(l1_store, "items", []) or [])
    previous_l2_items = dict(getattr(l2_store, "items", {}) or {}) if l2_store is not None else None
    previous_top_of_mind_refs = list(getattr(l2_store, "top_of_mind_refs", []) or []) if l2_store is not None else None
    previous_heat_registry = dict(getattr(l2_store, "heat_registry", {}) or {}) if l2_store is not None else None
    try:
        l1_store.items = [[
            L1TranscriptMessage(
                role="assistant",
                content=summary_entry.rendered or summary_entry.summary,
                kind=L1MessageKind.RUNTIME_CONTEXT_SUMMARY,
                payload=dict(summary_entry.payload or {}),
            )
        ]]
        remover = getattr(memory_service, "remove_projected_entries", None)
        if callable(remover):
            remover([SUMMARY_ENTRY_ID])
    except Exception:
        l1_store.items = previous_l1_items
        if l2_store is not None and previous_l2_items is not None:
            l2_store.items = previous_l2_items
        if l2_store is not None and previous_top_of_mind_refs is not None:
            l2_store.top_of_mind_refs = previous_top_of_mind_refs
        if l2_store is not None and previous_heat_registry is not None:
            l2_store.heat_registry = previous_heat_registry
        raise
    return MemoryCompactResult(
        summary=summary_entry.summary,
        projected_entries=[summary_entry],
        metadata={
            "target_input_budget": request.target_input_budget,
            "reserved_output_tokens": request.reserved_output_tokens,
            "projected_entry_count": 0,
            "compact_summary_count": 1,
            "retired_count": 0,
        },
    )


def coerce_minion_compaction_summary_entry(raw: Any, *, fallback_summary: str) -> L2Entry:
    payload = raw if isinstance(raw, dict) else {}
    schema = str(payload.get("schema") or "").strip()
    if schema != COMPACTION_SCHEMA_MINION_V1 and not fallback_summary:
        raise ValueError("minion structured compaction payload missing recognized schema")
    summary_payload = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    continuity = payload.get("continuity") if isinstance(payload.get("continuity"), dict) else {}
    summary_text = str(summary_payload.get("summary") or "").strip() or str(fallback_summary or "").strip()
    if not summary_text:
        raise ValueError("minion compact summary is empty")
    search_text = str(summary_payload.get("search_text") or "").strip() or summary_text
    summary_payload_blob = {
        "schema": COMPACTION_SCHEMA_MINION_V1,
        "kind": "minion",
        "continuity": dict(continuity),
        "summary": {
            "summary": summary_text,
            "search_text": search_text,
        },
    }
    return L2Entry(
        entry_id=SUMMARY_ENTRY_ID,
        kind="summary",
        scope="system",
        title=SUMMARY_TITLE,
        summary=summary_text,
        source_kind="l1_compaction",
        candidate_state="stable",
        touched_at=utc_now(),
        rendered=render_minion_compact_context_for_llm(summary=summary_text, payload=summary_payload_blob),
        search_text=search_text,
        payload=summary_payload_blob,
    )


def is_minion_compaction_payload(payload: object) -> bool:
    return isinstance(payload, dict) and str(payload.get("schema") or "").strip() == COMPACTION_SCHEMA_MINION_V1


def render_minion_compact_context_for_llm(*, summary: str, payload: dict[str, object]) -> str:
    continuity = payload.get("continuity") if isinstance(payload.get("continuity"), dict) else {}
    lines = [
        '<compact_context kind="minion" authority="reference_only">',
        "## Minion Task Continuity Reference",
        "",
        "This compact context is a continuity reference only, not source of truth.",
        "Verify against the work order, plan artifact, current milestone, checkpoint/ledger, and workspace before acting.",
        "",
    ]
    rendered = _render_minion_continuity(dict(continuity))
    if rendered:
        lines.append(rendered)
    summary_text = str(summary or "").strip()
    if summary_text:
        lines.extend(["", "### Summary", summary_text])
    lines.append("</compact_context>")
    return "\n".join(lines).strip()


def collect_minion_prior_user_inputs(items: list[list[L1TranscriptMessage]]) -> list[str]:
    result: list[str] = []
    for transcript in items:
        normalized = normalize_l1_transcript(transcript)
        if not normalized:
            continue
        if any(
            normalize_l1_message_kind(
                message.kind,
                role=str(message.role or "").strip(),
                tool_calls=message.tool_calls,
                tool_call_id=message.tool_call_id,
            )
            == L1MessageKind.RUNTIME_CONTEXT_SUMMARY
            for message in normalized
        ):
            continue
        for message in normalized:
            role = str(message.role or "").strip()
            kind = normalize_l1_message_kind(
                message.kind,
                role=role,
                tool_calls=message.tool_calls,
                tool_call_id=message.tool_call_id,
            )
            if role != "user" or kind != L1MessageKind.USER_REQUEST:
                continue
            content = _scrub_minion_management_info(str(message.content or "").strip())
            if content:
                result.append(content)
    return result


def _bounded_minion_prior_user_inputs(
    inputs: list[str],
    *,
    target_input_budget: int,
) -> tuple[list[str], int]:
    limit = max(MINION_COMPACT_MIN_PRIOR_USER_INPUT_BUDGET, int(target_input_budget or 0))
    result = [str(text or "").strip() for text in inputs if str(text or "").strip()]
    dropped = 0
    while result and len("\n\n".join(result)) > limit and len(result) > 1:
        result.pop(0)
        dropped += 1
    if result and len("\n\n".join(result)) > limit:
        result[0] = result[0][-limit:].lstrip()
        dropped += 1
    return result, dropped


def _scrub_minion_management_info(text: str) -> str:
    lines = []
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if _MINION_MANAGEMENT_LINE_RE.match(stripped):
            continue
        scrubbed_json = _scrub_json_line(stripped)
        if scrubbed_json is not None:
            if scrubbed_json:
                lines.append(scrubbed_json)
            continue
        lines.append(line.rstrip())
    return "\n".join(lines).strip()


def _scrub_json_line(text: str) -> str | None:
    if not text or text[0] not in "[{":
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    scrubbed = _scrub_management_keys(parsed)
    if scrubbed in ({}, []):
        return ""
    try:
        return json.dumps(scrubbed, ensure_ascii=False, sort_keys=True)
    except TypeError:
        return None


def _scrub_management_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _scrub_management_keys(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _MINION_MANAGEMENT_KEYS
        }
    if isinstance(value, list):
        return [_scrub_management_keys(item) for item in value]
    return value


def _render_minion_continuity(continuity: dict[str, object]) -> str:
    fields = (
        ("Prior Completed User Inputs", "prior_completed_user_inputs"),
        ("History Rule", "history_rule"),
        ("Current Turn Rule", "current_turn_rule"),
        ("Retired Prior User Input Count", "retired_prior_user_input_count"),
        ("Task Goal", "task_goal"),
        ("Current Milestone Hint", "current_milestone_hint"),
        ("Claimed Completed", "claimed_completed"),
        ("Claimed Pending", "claimed_pending"),
        ("Implementation Decisions", "implementation_decisions"),
        ("Verification Hints", "verification_hints"),
        ("Review Or Repair Hints", "review_or_repair_hints"),
        ("Must Verify Against", "must_verify_against"),
        ("Next Action Hint", "next_action_hint"),
    )
    return _render_compact_sections(fields=fields, continuity=continuity)


def _render_compact_sections(*, fields: tuple[tuple[str, str], ...], continuity: dict[str, object]) -> str:
    sections: list[str] = []
    for title, key in fields:
        value = continuity.get(key)
        rendered = _render_markdown_value(value)
        if rendered:
            sections.extend([f"### {title}", rendered])
    return "\n\n".join(sections).strip()


def _render_markdown_value(value: object) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, list):
        items = []
        for item in value[:12]:
            text = _markdown_item_text(item)
            if text:
                items.append(f"- {text}")
        return "\n".join(items)
    if isinstance(value, dict):
        items = []
        for key, item in value.items():
            text = _markdown_item_text(item)
            if text:
                items.append(f"- {key}: {text}")
        return "\n".join(items)
    return str(value).strip()


def _markdown_item_text(value: object) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, dict):
        for key in ("summary", "title", "text", "path", "id"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return ", ".join(f"{key}={item}" for key, item in value.items() if item not in (None, "", []))
    return str(value).strip()

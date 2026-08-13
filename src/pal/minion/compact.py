from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from pal.core.compaction import (
    CompactionClockKind,
    CompactionSnapshot,
    CompactionUnit,
    compaction_visible_token_limit,
    extract_json_object,
)
from pal.foundation import utc_now
from pal.memory.compact import SUMMARY_ENTRY_ID, SUMMARY_TITLE
from pal.memory.contracts import L2Entry

COMPACTION_SCHEMA_MINION_V3 = "pal.compaction.minion.v3"

MINION_COMPACTION_SYSTEM_PROMPT = (
    "You are the Minion work-checkpoint compactor.\n"
    "Return one valid JSON object only, using schema pal.compaction.minion.v3.\n"
    "The checkpoint records the exact working cursor, not the role's source-of-truth assignment and not private chain-of-thought.\n"
    "Do not reconstruct or replay the role assignment; record only execution continuity.\n"
    "Required top-level fields:\n"
    '- "schema": "pal.compaction.minion.v3"\n'
    '- "kind": "minion"\n'
    '- "continuity": an object containing all five fields below\n'
    '- "summary": {"summary": non-empty string, "search_text": non-empty string}\n'
    "continuity fields:\n"
    "- technical_route: current technical route and the evidence-based reason for it.\n"
    "- active_work: current goal, concrete file/symbol target, action, and status.\n"
    "- active_errors: symptom, latest evidence, and current hypothesis.\n"
    "- active_issues: issue, known facts, status, and paths already excluded.\n"
    "- next_actions: next concrete action, target, and expected result.\n"
    "Every continuity field is an array. Every item must contain exactly the keys named above; empty arrays are valid.\n"
    'Valid minimal example: {"schema":"pal.compaction.minion.v3","kind":"minion","continuity":{"technical_route":[],"active_work":[],"active_errors":[],"active_issues":[],"next_actions":[]},"summary":{"summary":"No active work remains.","search_text":"no active work"}}\n'
    "Do not emit memory_candidates. Do not include hidden reasoning, internal deliberation, chain-of-thought, or a prose replay of the role assignment.\n"
    "Record failed, rejected, and unknown-effect tool work accurately; do not claim its side effects succeeded.\n"
    "Closed tool batches may be compressed into verified work state. Never invent a tool result.\n"
    "Checklist state is the macro plan; this checkpoint is the precise cursor within it.\n"
    "Obey the request-specific visible checkpoint limit below. It applies to the JSON, not private reasoning.\n"
    "Output JSON only, without markdown fences."
)

_CONTINUITY_FIELDS = (
    "technical_route",
    "active_work",
    "active_errors",
    "active_issues",
    "next_actions",
)
_CONTINUITY_ITEM_FIELDS = {
    "technical_route": frozenset({"route", "rationale"}),
    "active_work": frozenset({"goal", "target", "action", "status"}),
    "active_errors": frozenset(
        {"symptom", "latest_evidence", "current_hypothesis"}
    ),
    "active_issues": frozenset(
        {"issue", "known_facts", "status", "excluded_paths"}
    ),
    "next_actions": frozenset({"action", "target", "expected_result"}),
}
_MINION_TOP_LEVEL_FIELDS = frozenset(
    {"schema", "kind", "continuity", "summary"}
)
_FORBIDDEN_REASONING_KEYS = frozenset(
    {
        "chain_of_thought",
        "chain-of-thought",
        "cot",
        "hidden_reasoning",
        "internal_reasoning",
        "deliberation",
    }
)


@dataclass(frozen=True)
class MinionCompactionPolicy:
    policy_id: str = COMPACTION_SCHEMA_MINION_V3
    clock_kind: CompactionClockKind = CompactionClockKind.LLM_ROUND
    accepts_memory_candidates: bool = False

    def system_prompt(self, snapshot: CompactionSnapshot) -> str:
        limit = compaction_visible_token_limit(snapshot)
        return (
            f"{MINION_COMPACTION_SYSTEM_PROMPT}\n"
            f"The final visible JSON checkpoint for this request must not exceed {limit:,} tokens."
        )

    def build_source(
        self,
        snapshot: CompactionSnapshot,
        units: Sequence[CompactionUnit],
        *,
        validation_error: str = "",
    ) -> str:
        lines = [
            '<compact_source kind="minion" schema_target="pal.compaction.minion.v3">',
            "## Source Semantics",
            "",
            "- Frozen L1 below is the only compaction truth source.",
            "- External module contracts, checklist files, code, and recalled memory remain projected outside compact.",
            "- The previous compact seed, when present, is already part of frozen L1.",
            "- Atomic L1 units may not be split. Record uncertain outcomes without claiming success.",
            "- Preserve exact files, symbols, commands, latest error evidence, excluded paths, and the next executable action.",
        ]
        if validation_error:
            lines.extend(
                [
                    "",
                    "## Previous Output Validation Error",
                    validation_error,
                    "Return a complete corrected JSON object.",
                ]
            )
        lines.extend(
            [
                "",
                "## Previous Compact Seed",
                _previous_seed(snapshot.previous_summary),
                "",
                "## Frozen L1 Work History",
            ]
        )
        if not units:
            lines.append("No ordinary work-history units remain.")
        for unit in units:
            lines.extend(
                [
                    "",
                    f"### {unit.unit_id} source={unit.source} state=closed",
                    unit.text or "[empty unit]",
                ]
            )
        lines.append("</compact_source>")
        return "\n".join(lines).strip()

    def validate_checkpoint(
        self,
        raw_text: str,
        snapshot: CompactionSnapshot,
    ) -> L2Entry:
        _ = snapshot
        payload = extract_json_object(raw_text)
        if str(payload.get("schema") or "").strip() != self.policy_id:
            raise ValueError(f"schema must be {self.policy_id}")
        if str(payload.get("kind") or "").strip().lower() != "minion":
            raise ValueError("kind must be minion")
        if "memory_candidates" in payload:
            raise ValueError("minion checkpoints must not contain memory_candidates")
        forbidden = _find_forbidden_reasoning_key(payload)
        if forbidden:
            raise ValueError(f"forbidden reasoning field: {forbidden}")
        _reject_extra_fields(payload, _MINION_TOP_LEVEL_FIELDS, "checkpoint")
        continuity = payload.get("continuity")
        if not isinstance(continuity, dict):
            raise ValueError("continuity must be an object")
        missing = [
            key for key in _CONTINUITY_FIELDS if key not in continuity
        ]
        if missing:
            raise ValueError(
                "continuity missing fields: " + ", ".join(missing)
            )
        _reject_extra_fields(
            continuity,
            frozenset(_CONTINUITY_FIELDS),
            "continuity",
        )
        for key in _CONTINUITY_FIELDS:
            _validate_continuity_items(key, continuity.get(key))
        summary = payload.get("summary")
        if not isinstance(summary, dict):
            raise ValueError("summary must be an object")
        _reject_extra_fields(
            summary,
            frozenset({"summary", "search_text"}),
            "summary",
        )
        summary_text = _require_nonempty_string(summary, "summary", "summary")
        search_text = _require_nonempty_string(
            summary,
            "search_text",
            "summary",
        )
        normalized_payload: dict[str, Any] = {
            "schema": self.policy_id,
            "kind": "minion",
            "continuity": {
                key: _normalize_checkpoint_field(continuity.get(key))
                for key in _CONTINUITY_FIELDS
            },
            "summary": {
                "summary": summary_text,
                "search_text": search_text,
            },
        }
        return _make_minion_summary_entry(normalized_payload)


def render_minion_compact_context_for_llm(
    *,
    summary: str,
    payload: dict[str, object],
) -> str:
    continuity = (
        payload.get("continuity")
        if isinstance(payload.get("continuity"), dict)
        else {}
    )
    lines = [
        '<compact_context kind="minion" authority="work_checkpoint">',
        "## Minion Work Checkpoint",
        "",
        "This is a compact work cursor, not the role assignment and not hidden reasoning.",
        "The mechanically projected role input, bound work view, contracts, checklist, workspace, and durable protocol journal remain authoritative.",
    ]
    for title, key in (
        ("Technical Route", "technical_route"),
        ("Active Work", "active_work"),
        ("Active Errors", "active_errors"),
        ("Active Issues", "active_issues"),
        ("Next Actions", "next_actions"),
    ):
        rendered = _render_markdown_value(continuity.get(key))
        if rendered:
            lines.extend(["", f"### {title}", rendered])
    summary_text = str(summary or "").strip()
    if summary_text:
        lines.extend(["", "### Summary", summary_text])
    lines.append("</compact_context>")
    return "\n".join(lines).strip()


def is_minion_compaction_payload(payload: object) -> bool:
    return (
        isinstance(payload, dict)
        and str(payload.get("schema") or "").strip()
        == COMPACTION_SCHEMA_MINION_V3
    )


def _make_minion_summary_entry(payload: dict[str, Any]) -> L2Entry:
    summary_payload = dict(payload.get("summary") or {})
    summary = str(summary_payload.get("summary") or "").strip()
    search_text = str(summary_payload.get("search_text") or "").strip()
    return L2Entry(
        entry_id=SUMMARY_ENTRY_ID,
        kind="summary",
        scope="system",
        title=SUMMARY_TITLE,
        summary=summary,
        source_kind="l1_compaction",
        candidate_state="stable",
        touched_at=utc_now(),
        rendered=render_minion_compact_context_for_llm(
            summary=summary,
            payload=payload,
        ),
        search_text=search_text or summary,
        payload=dict(payload),
    )


def _previous_seed(entry: L2Entry | None) -> str:
    if entry is None:
        return "No previous compact seed."
    payload = dict(entry.payload or {})
    if payload:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    return entry.rendered or entry.summary or "Previous seed was empty."


def _normalize_checkpoint_field(value: Any) -> list[Any]:
    return [_copy_json_value(item) for item in list(value or ())]


def _copy_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _copy_json_value(item)
            for key, item in value.items()
            if str(key).strip()
        }
    if isinstance(value, list):
        return [_copy_json_value(item) for item in value]
    return value


def _validate_continuity_items(key: str, value: Any) -> None:
    if not isinstance(value, list):
        raise ValueError(f"continuity.{key} must be an array")
    allowed = _CONTINUITY_ITEM_FIELDS[key]
    for index, item in enumerate(value):
        label = f"continuity.{key}[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{label} must be an object")
        _reject_extra_fields(item, allowed, label)
        missing = sorted(allowed - set(item))
        if missing:
            raise ValueError(f"{label} missing fields: " + ", ".join(missing))
        for field_name in allowed:
            field_value = item.get(field_name)
            if key == "active_issues" and field_name in {
                "known_facts",
                "excluded_paths",
            }:
                if not isinstance(field_value, list) or not all(
                    isinstance(entry, str) and entry.strip()
                    for entry in field_value
                ):
                    raise ValueError(
                        f"{label}.{field_name} must be an array of non-empty strings"
                    )
                continue
            if not isinstance(field_value, str) or not field_value.strip():
                raise ValueError(
                    f"{label}.{field_name} must be a non-empty string"
                )


def _require_nonempty_string(
    value: dict[str, Any],
    key: str,
    label: str,
) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{label}.{key} must be a non-empty string")
    return item.strip()


def _reject_extra_fields(
    value: dict[str, Any],
    allowed: frozenset[str],
    label: str,
) -> None:
    extras = sorted(str(key) for key in set(value) - set(allowed))
    if extras:
        raise ValueError(f"{label} has extra fields: " + ", ".join(extras))


def _find_forbidden_reasoning_key(value: Any) -> str:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key or "").strip().lower()
            if normalized in _FORBIDDEN_REASONING_KEYS:
                return normalized
            nested = _find_forbidden_reasoning_key(item)
            if nested:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _find_forbidden_reasoning_key(item)
            if nested:
                return nested
    return ""


def _render_markdown_value(value: object) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, list):
        items = []
        for item in value[:16]:
            text = _markdown_item_text(item)
            if text:
                items.append(f"- {text}")
        return "\n".join(items)
    return _markdown_item_text(value)


def _markdown_item_text(value: object) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, dict):
        return "; ".join(
            f"{key}: {_markdown_item_text(item)}"
            for key, item in value.items()
            if item not in (None, "", [])
        )
    if isinstance(value, list):
        return ", ".join(
            text
            for item in value
            if (text := _markdown_item_text(item))
        )
    return str(value).strip()


__all__ = [
    "COMPACTION_SCHEMA_MINION_V3",
    "MINION_COMPACTION_SYSTEM_PROMPT",
    "MinionCompactionPolicy",
    "is_minion_compaction_payload",
    "render_minion_compact_context_for_llm",
]

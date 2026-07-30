from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from pal.core.compaction import (
    CompactionClockKind,
    CompactionSnapshot,
    CompactionUnit,
    extract_json_object,
)
from pal.foundation import utc_now
from pal.memory.compact import (
    SUMMARY_ENTRY_ID,
    SUMMARY_TITLE,
    coerce_memory_candidate_list,
)
from pal.memory.contracts import L2Entry

COMPACTION_SCHEMA_PAL_V2 = "pal.compaction.pal.v2"
_PAL_CONTINUITY_STRING_FIELDS = (
    "current_focus",
    "primary_request_and_intent",
    "optional_next_step",
)
_PAL_CONTINUITY_LIST_FIELDS = (
    "active_operating_instructions",
    "active_requests",
    "temporary_task_state",
    "key_decisions",
    "pending_questions",
    "recent_raw_turns",
    "warm_compressed_turns",
    "retired_or_superseded_context",
)
_PAL_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema",
        "kind",
        "continuity",
        "summary",
        "memory_candidates",
        "degraded",
        "degraded_failures",
    }
)
_PAL_MEMORY_CANDIDATE_FIELDS = frozenset(
    {
        "kind",
        "title",
        "summary",
        "source_excerpt",
        "why_durable",
        "confidence",
        "task_id",
        "topics",
        "star",
    }
)

COMPACT_PAL_STRUCTURED_SYSTEM = (
    "You are a memory compaction engine.\n"
    "Read the XML/Markdown compact source below and produce a structured JSON object using schema pal.compaction.pal.v2.\n"
    "This is not a vibe summary. Your job is to keep the conversation alive after old context is deleted.\n"
    "Be sharp, literal, and ruthless about lifecycle: active stays active; completed, contradicted, or stale context is retired.\n"
    "\n"
    "Required top-level fields:\n"
    '  "schema": "pal.compaction.pal.v2"\n'
    '  "kind": "pal"\n'
    '  "continuity": object with fields:\n'
    "    - current_focus (string): the live topic or collaboration thread.\n"
    "    - primary_request_and_intent (string): what the user is trying to accomplish, not generic backstory.\n"
    "    - active_operating_instructions (array): HOW Pal must work right now.\n"
    "    - active_requests (array): WHAT the user currently wants Pal to do.\n"
    "    - temporary_task_state (array): ephemeral progress needed to resume this thread.\n"
    "    - key_decisions (array): decisions confirmed in this conversation that still matter.\n"
    "    - pending_questions (array): unresolved questions or tradeoffs.\n"
    "    - recent_raw_turns (array): preserve the most recent user/assistant turns that must survive nearly verbatim.\n"
    "    - warm_compressed_turns (array): lightly compressed older turns; preserve intent, constraints, and pivots.\n"
    "    - retired_or_superseded_context (array): completed, cancelled, contradicted, or stale context that must not drive behavior.\n"
    "    - optional_next_step (string): the next action, anchored in a recent user quote or near-quote when available.\n"
    '  "summary": a JSON object with fields:\n'
    "    - summary (string, required): compact continuity summary for prompt display\n"
    "    - search_text (string, required): compact source excerpts, identifiers, and key terms for retrieval; do not dump full transcripts\n"
    '  "memory_candidates": a list of zero or more candidate items, each with:\n'
    '    - kind (string): "fact" or "case"\n'
    "    - title (string, required): short label identifying the candidate\n"
    "    - summary (string, required): concise candidate summary for future LLM consumption\n"
    "    - source_excerpt (string, required): short source excerpt or key terms justifying the candidate\n"
    "    - why_durable (string, optional): why this may be worth long-term memory\n"
    "    - confidence (string, optional): low, medium, or high\n"
    "    - task_id (string, optional)\n"
    "    - topics (array, optional): short topic tags\n"
    "    - star (object, required for case kind, omitted for fact kind): situation/task/action/result strings\n"
    "\n"
    "Field boundaries. Do not mix these up:\n"
    "- active_operating_instructions = HOW Pal should work. Examples: 'plan first, do not edit code yet'; 'do not touch L3'; 'render prompt as XML+Markdown, not JSON'. Not a concrete implementation task.\n"
    "- active_requests = WHAT Pal should do. Examples: 'implement Pal compact v2'; 'add real-LLM compaction tests'; 'route automatic compact candidates through approval'. Not a style rule.\n"
    "- temporary_task_state = ephemeral progress needed to resume. Examples: 'hot/raw=5 and warm=20 are chosen'; 'Minion is out of scope'; 'automatic approval delivery still needs wiring'. It expires when done, cancelled, or superseded.\n"
    "- retired_or_superseded_context = things the model must stop using. Examples: old tasks already completed, old assumptions corrected by the user, context older than the warm window with no active effect.\n"
    "\n"
    "Rules:\n"
    "- Output valid JSON only, no markdown fences.\n"
    "- Do not output XML or Markdown. The policy renderer turns your JSON into XML-wrapped Markdown later.\n"
    "- Previous compact data is already lossy. Carry forward only still-active seed facts not contradicted by recent turns.\n"
    "- Preserve recent user wording when it contains constraints, corrections, or next-step anchors.\n"
    "- optional_next_step must quote or near-quote the user's recent instruction when possible; if no reliable next step exists, say so.\n"
    "- Keep temporary task state temporary. Do not turn it into a user preference, durable fact, or permanent task.\n"
    "- If a task is complete, cancelled, superseded, or contradicted by newer user text, move it to retired_or_superseded_context.\n"
    "- Write a complete bounded continuity summary. Do not trail off or rely on output continuation.\n"
    "- Keep the final visible JSON checkpoint at or below 20,000 tokens. This limit applies to the JSON, not private reasoning.\n"
    "- If the source is long, preserve active continuity first, then user constraints, decisions, and warm history.\n"
    "- Prioritize durable user preferences, stable user status/context, real goals/plans/commitments, confirmed project decisions, and long-lived constraints.\n"
    "- Do not create entries from jokes, temporary emotions, momentary frustration, speculation, transient runtime state, or unconfirmed intent.\n"
    "- Do not invent information not present in the source.\n"
    "- summary is always required and should cover the recoverable context.\n"
    "- Create memory_candidates only for stable facts, preferences, status/context, goals/plans, commitments, project facts, confirmed decisions, or explicitly reusable task/project cases.\n"
    "- For memory_candidates with kind='case', star is mandatory and must contain non-empty situation, task, action, and result strings.\n"
    "- For memory_candidates with kind='fact', omit star.\n"
    "- memory_candidates are candidates only; automatic and manual compact candidates both require approval.\n"
    "- Do not create memory_candidates from temporary task state or todos unless the user explicitly asked Pal to remember them.\n"
    "- Do not create memory_candidates for repair lessons, procedures, behavior rules, routing advice, or skill workflows unless the user explicitly asked to remember them as memory.\n"
    "- If nothing is worth extracting, return an empty memory_candidates list.\n"
    "- title, summary, and source_excerpt/search_text serve different purposes: title is a short label; summary is compressed prompt content; source_excerpt/search_text are retrieval/audit terms.\n"
    "The Pal compact tracks the user and current collaboration continuity."
)


@dataclass(frozen=True)
class PalCompactionPolicy:
    policy_id: str = COMPACTION_SCHEMA_PAL_V2
    clock_kind: CompactionClockKind = CompactionClockKind.USER_TURN
    accepts_memory_candidates: bool = True

    def system_prompt(self, snapshot: CompactionSnapshot) -> str:
        _ = snapshot
        return COMPACT_PAL_STRUCTURED_SYSTEM

    def build_source(
        self,
        snapshot: CompactionSnapshot,
        units: Sequence[CompactionUnit],
        *,
        validation_error: str = "",
    ) -> str:
        lines = [
            '<compact_source kind="pal" schema_target="pal.compaction.pal.v2">',
            "## Source Semantics",
            "",
            "- Frozen L1 below is the only compaction truth source.",
            "- The previous compact seed, when present, is already part of frozen L1.",
            "- Every L1 unit below is atomic. Never split a tool call from its result.",
            "- Recovery-required units describe failed, rejected, unknown-effect, or incomplete work and must preserve recovery meaning.",
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
            ]
        )
        lines.extend(["", "## Frozen L1 Units"])
        if not units:
            lines.append("No ordinary history units remain.")
        for unit in units:
            state = (
                "recovery_required"
                if unit.recovery_required
                else "closed_success"
            )
            lines.extend(
                [
                    "",
                    f"### {unit.unit_id} source={unit.source} state={state}",
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
        _validate_pal_checkpoint_payload(payload, policy_id=self.policy_id)
        return _make_pal_summary_entry(payload)

    def degraded_checkpoint(
        self,
        snapshot: CompactionSnapshot,
        units: Sequence[CompactionUnit],
        *,
        failures: Sequence[str],
    ) -> L2Entry:
        tail = list(units)[-3:]
        recovery = [unit for unit in units if unit.recovery_required]
        previous = _previous_pal_continuity(snapshot.previous_summary)
        recent_text = [_bounded_text(unit.text, limit=1200) for unit in tail]
        temporary_state = [
            {
                "source": unit.source,
                "state": (
                    "recovery_required"
                    if unit.recovery_required
                    else "recent_safe_tail"
                ),
                "text": _bounded_text(unit.text, limit=1200),
            }
            for unit in _dedupe_units([*recovery, *tail])
        ]
        summary_text = (
            "Degraded compaction checkpoint: semantic generation did not produce a valid checkpoint. "
            "The previous seed, recent L1 tail, and recovery-required work were retained mechanically."
        )
        payload: dict[str, Any] = {
            "schema": self.policy_id,
            "kind": "pal",
            "degraded": True,
            "continuity": {
                "current_focus": str(previous.get("current_focus") or _latest_l1_text(units, limit=600)),
                "primary_request_and_intent": str(
                    previous.get("primary_request_and_intent")
                    or _latest_l1_text(units, limit=1200)
                ),
                "active_operating_instructions": list(previous.get("active_operating_instructions") or ()),
                "active_requests": list(previous.get("active_requests") or ()),
                "temporary_task_state": [
                    *list(previous.get("temporary_task_state") or ()),
                    *temporary_state,
                ],
                "key_decisions": list(previous.get("key_decisions") or ()),
                "pending_questions": list(previous.get("pending_questions") or ()),
                "recent_raw_turns": [
                    *list(previous.get("recent_raw_turns") or ())[-3:],
                    *recent_text,
                ],
                "warm_compressed_turns": list(previous.get("warm_compressed_turns") or ()),
                "retired_or_superseded_context": list(
                    previous.get("retired_or_superseded_context") or ()
                ),
                "optional_next_step": str(
                    previous.get("optional_next_step")
                    or "Reconcile every recovery-required L1 unit before repeating side effects."
                ),
            },
            "summary": {
                "summary": summary_text,
                "search_text": "\n".join(
                    [
                        _previous_seed(snapshot.previous_summary),
                        *(unit.text for unit in recovery),
                        *(unit.text for unit in tail),
                    ]
                ).strip()
                or summary_text,
            },
            "memory_candidates": [],
            "degraded_failures": [
                str(item)[:240] for item in failures[-3:]
            ],
        }
        return self.validate_checkpoint(
            json.dumps(payload, ensure_ascii=False),
            snapshot,
        )


def _make_pal_summary_entry(payload: dict[str, Any]) -> L2Entry:
    summary_payload = dict(payload.get("summary") or {})
    continuity = (
        dict(payload.get("continuity") or {})
        if isinstance(payload.get("continuity"), dict)
        else {}
    )
    summary = str(summary_payload.get("summary") or "").strip()
    search_text = (
        str(summary_payload.get("search_text") or "").strip() or summary
    )
    normalized_payload: dict[str, Any] = {
        "schema": COMPACTION_SCHEMA_PAL_V2,
        "kind": "pal",
        "continuity": _normalize_pal_v2_continuity(continuity),
        "summary": {
            "summary": summary,
            "search_text": search_text,
        },
        "memory_candidates": coerce_memory_candidate_list(
            payload.get("memory_candidates")
        ),
    }
    if bool(payload.get("degraded")):
        normalized_payload["degraded"] = True
        normalized_payload["degraded_failures"] = [
            str(item)[:240]
            for item in list(payload.get("degraded_failures") or ())[-3:]
            if str(item)
        ]
    return L2Entry(
        entry_id=SUMMARY_ENTRY_ID,
        kind="summary",
        scope="system",
        title=SUMMARY_TITLE,
        summary=summary,
        source_kind="l1_compaction",
        candidate_state="stable",
        touched_at=utc_now(),
        rendered=_render_pal_compact_context(
            summary=summary,
            payload=normalized_payload,
        ),
        search_text=search_text,
        payload=normalized_payload,
    )


def _validate_pal_checkpoint_payload(
    payload: dict[str, Any],
    *,
    policy_id: str,
) -> None:
    _reject_extra_fields(payload, _PAL_TOP_LEVEL_FIELDS, "checkpoint")
    if str(payload.get("schema") or "").strip() != policy_id:
        raise ValueError(f"schema must be {policy_id}")
    if str(payload.get("kind") or "").strip().lower() != "pal":
        raise ValueError("kind must be pal")

    continuity = payload.get("continuity")
    if not isinstance(continuity, dict):
        raise ValueError("continuity must be an object")
    expected_continuity = frozenset(
        (*_PAL_CONTINUITY_STRING_FIELDS, *_PAL_CONTINUITY_LIST_FIELDS)
    )
    _reject_extra_fields(continuity, expected_continuity, "continuity")
    missing = sorted(expected_continuity - set(continuity))
    if missing:
        raise ValueError("continuity missing fields: " + ", ".join(missing))
    for key in _PAL_CONTINUITY_STRING_FIELDS:
        if not isinstance(continuity.get(key), str):
            raise ValueError(f"continuity.{key} must be a string")
    for key in _PAL_CONTINUITY_LIST_FIELDS:
        if not isinstance(continuity.get(key), list):
            raise ValueError(f"continuity.{key} must be an array")

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise ValueError("summary must be an object")
    _reject_extra_fields(summary, frozenset({"summary", "search_text"}), "summary")
    _require_nonempty_string(summary, "summary", "summary")
    _require_nonempty_string(summary, "search_text", "summary")

    candidates = payload.get("memory_candidates")
    if not isinstance(candidates, list):
        raise ValueError("memory_candidates must be an array")
    for index, candidate in enumerate(candidates):
        _validate_pal_memory_candidate(candidate, index=index)

    if "degraded" in payload and not isinstance(payload.get("degraded"), bool):
        raise ValueError("degraded must be a boolean")
    if "degraded_failures" in payload:
        failures = payload.get("degraded_failures")
        if not isinstance(failures, list) or not all(
            isinstance(item, str) for item in failures
        ):
            raise ValueError("degraded_failures must be an array of strings")


def _validate_pal_memory_candidate(candidate: Any, *, index: int) -> None:
    label = f"memory_candidates[{index}]"
    if not isinstance(candidate, dict):
        raise ValueError(f"{label} must be an object")
    _reject_extra_fields(candidate, _PAL_MEMORY_CANDIDATE_FIELDS, label)
    kind = _require_nonempty_string(candidate, "kind", label)
    if kind not in {"fact", "case"}:
        raise ValueError(f"{label}.kind must be fact or case")
    for key in ("title", "summary", "source_excerpt"):
        _require_nonempty_string(candidate, key, label)
    for key in ("why_durable", "confidence", "task_id"):
        if key in candidate and not isinstance(candidate.get(key), str):
            raise ValueError(f"{label}.{key} must be a string")
    if "topics" in candidate:
        topics = candidate.get("topics")
        if not isinstance(topics, list) or not all(
            isinstance(item, str) and item.strip() for item in topics
        ):
            raise ValueError(f"{label}.topics must be an array of non-empty strings")
    star = candidate.get("star")
    if kind == "fact":
        if "star" in candidate:
            raise ValueError(f"{label}.star must be omitted for fact")
        return
    if not isinstance(star, dict):
        raise ValueError(f"{label}.star is required for case")
    _reject_extra_fields(
        star,
        frozenset({"situation", "task", "action", "result"}),
        f"{label}.star",
    )
    for key in ("situation", "task", "action", "result"):
        _require_nonempty_string(star, key, f"{label}.star")


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


def _render_pal_compact_context(
    *,
    summary: str,
    payload: dict[str, Any],
) -> str:
    continuity = (
        dict(payload.get("continuity") or {})
        if isinstance(payload.get("continuity"), dict)
        else {}
    )
    lines = [
        '<compact_context kind="pal" authority="conversation_continuity">',
        "## Conversation Continuity",
        "",
        "This is compressed prior conversation context, not a new user request.",
        "Use it to resume the current collaboration thread. Treat task state as temporary, not durable memory.",
        "Retire completed, superseded, or user-cancelled tasks when newer context contradicts them.",
        "",
    ]
    sections = (
        ("Current Focus", "current_focus"),
        ("Primary Request And Intent", "primary_request_and_intent"),
        ("Active Operating Instructions", "active_operating_instructions"),
        ("Active Requests", "active_requests"),
        ("Temporary Task State", "temporary_task_state"),
        ("Key Decisions", "key_decisions"),
        ("Pending Questions", "pending_questions"),
        ("Recent Raw Turns", "recent_raw_turns"),
        ("Warm Compressed Turns", "warm_compressed_turns"),
        (
            "Retired Or Superseded Context",
            "retired_or_superseded_context",
        ),
        ("Optional Next Step", "optional_next_step"),
    )
    for title, key in sections:
        rendered = _render_markdown_value(continuity.get(key))
        if rendered:
            lines.extend([f"### {title}", rendered, ""])
    summary_text = str(summary or "").strip()
    if summary_text:
        lines.extend(["### Summary", summary_text])
    candidates = payload.get("memory_candidates")
    if isinstance(candidates, list) and candidates:
        lines.extend(["", "### Durable Memory Candidates Pending Approval"])
        for item in candidates[:8]:
            if not isinstance(item, dict):
                continue
            title = str(
                item.get("title")
                or item.get("summary")
                or "candidate"
            ).strip()
            kind = str(item.get("kind") or "candidate").strip()
            lines.append(f"- {kind}: {title}")
    if bool(payload.get("degraded")):
        lines.extend(
            [
                "",
                "### Checkpoint Quality",
                "degraded: reconcile the current request and recovery-required state before repeating side effects.",
            ]
        )
    lines.append("</compact_context>")
    return "\n".join(lines).strip()


def _normalize_pal_v2_continuity(
    continuity: dict[str, Any],
) -> dict[str, Any]:
    source = dict(continuity or {})
    return {
        "current_focus": _first_present(source, "current_focus"),
        "primary_request_and_intent": _first_present(
            source,
            "primary_request_and_intent",
        ),
        "active_operating_instructions": _listish(
            source.get("active_operating_instructions")
        ),
        "active_requests": _listish(source.get("active_requests")),
        "temporary_task_state": _listish(
            source.get("temporary_task_state")
        ),
        "key_decisions": _listish(source.get("key_decisions")),
        "pending_questions": _listish(source.get("pending_questions")),
        "recent_raw_turns": _listish(source.get("recent_raw_turns")),
        "warm_compressed_turns": _listish(
            source.get("warm_compressed_turns")
        ),
        "retired_or_superseded_context": _listish(
            source.get("retired_or_superseded_context")
        ),
        "optional_next_step": _first_present(
            source,
            "optional_next_step",
        ),
    }


def _render_markdown_value(value: Any) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, list):
        return "\n".join(
            f"- {text}"
            for item in value[:12]
            if (text := _markdown_item_text(item))
        )
    if isinstance(value, dict):
        return "\n".join(
            f"- {key}: {text}"
            for key, item in value.items()
            if (text := _markdown_item_text(item))
        )
    return str(value).strip()


def _markdown_item_text(value: Any) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, dict):
        for key in ("summary", "title", "text", "path", "id"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        return ", ".join(
            f"{key}={item}"
            for key, item in value.items()
            if item not in (None, "", [])
        )
    return str(value).strip()


def _first_present(source: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value not in (None, "", []):
            return str(value).strip()
    return ""


def _listish(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple)):
        return [item for item in value if item not in (None, "")]
    return [value]


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


def _previous_pal_continuity(entry: L2Entry | None) -> dict[str, Any]:
    if entry is None:
        return {}
    payload = dict(entry.payload or {})
    continuity = payload.get("continuity")
    if not isinstance(continuity, dict):
        return {}
    return _normalize_pal_v2_continuity(continuity)


def _latest_l1_text(
    units: Sequence[CompactionUnit],
    *,
    limit: int,
) -> str:
    for unit in reversed(list(units)):
        text = _bounded_text(unit.text, limit=limit)
        if text:
            return text
    return ""


def _bounded_text(value: str, *, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    head = max(1, limit // 2)
    tail = max(1, limit - head)
    return (
        text[:head].rstrip()
        + f"\n[... omitted {len(text) - head - tail} chars ...]\n"
        + text[-tail:].lstrip()
    )


def _dedupe_units(units: Sequence[CompactionUnit]) -> list[CompactionUnit]:
    result: list[CompactionUnit] = []
    seen: set[str] = set()
    for unit in units:
        if unit.unit_id in seen:
            continue
        seen.add(unit.unit_id)
        result.append(unit)
    return result


__all__ = [
    "COMPACTION_SCHEMA_PAL_V2",
    "COMPACT_PAL_STRUCTURED_SYSTEM",
    "PalCompactionPolicy",
]

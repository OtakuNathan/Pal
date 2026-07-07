from __future__ import annotations

import re
from typing import Any

from pal.foundation import utc_now
from pal.memory.candidates import memory_star_from_args
from pal.memory.contracts import CompactionProfile, L1MessageKind, L1TranscriptMessage, L2Entry

COMPACTION_SCHEMA_PAL_V2 = "pal.compaction.pal.v2"
COMPACTION_SCHEMAS = {COMPACTION_SCHEMA_PAL_V2}

SUMMARY_ENTRY_ID = "memory_summary_current"
SUMMARY_TITLE = "Conversation Summary"
PAL_COMPACT_HOT_RAW_TURNS = 5
PAL_COMPACT_WARM_TURNS = 20

_COMPACTABLE_L1_MESSAGE_KINDS = frozenset({
    L1MessageKind.USER_REQUEST,
    L1MessageKind.ASSISTANT_REPLY,
    L1MessageKind.TURN_INTERRUPTED,
    L1MessageKind.TURN_ABORTED,
})

_PERSISTENT_SYSTEM_REMINDER_RE = re.compile(
    r"\s*<system-reminder\b[^>]*>.*?</system-reminder>\s*",
    re.IGNORECASE | re.DOTALL,
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
    "- Do not output XML or Markdown. The runtime renderer will turn your JSON into XML-wrapped Markdown later.\n"
    "- Previous compact data is already lossy. Do not summarize the previous compact rendered text as transcript; only carry forward still-active seed facts not contradicted by recent turns.\n"
    "- Preserve recent user wording when it contains constraints, corrections, or next-step anchors.\n"
    "- optional_next_step must quote or near-quote the user's recent instruction when possible; if no reliable next step exists, say so.\n"
    "- Keep temporary task state temporary. Do not turn it into a user preference, durable fact, or permanent task.\n"
    "- If a task is complete, cancelled, superseded, or contradicted by newer user text, move it to retired_or_superseded_context.\n"
    "- Write a complete bounded continuity summary. Do not trail off, end mid-sentence, or rely on output truncation.\n"
    "- If the source is long, preserve active continuity first, then user constraints, then decisions, then warm history.\n"
    "- Prioritize durable user preferences, stable user status/context, real goals/plans/commitments, confirmed project decisions, and long-lived constraints.\n"
    "- Do not create entries from jokes, temporary emotions, momentary frustration, speculation, transient runtime state, or unconfirmed intent.\n"
    "- Do not invent information not present in the source.\n"
    "- summary is always required and should cover the recoverable context.\n"
    "- Create memory_candidates only for stable facts, preferences, status/context, goals/plans, commitments, project facts, confirmed decisions, or explicitly reusable task/project cases.\n"
    "- For memory_candidates with kind='case', star is mandatory and must contain non-empty situation, task, action, and result strings.\n"
    "- For memory_candidates with kind='fact', omit star.\n"
    "- memory_candidates are candidates only; they are not committed long-term memory. Automatic and manual compact candidates both require approval.\n"
    "- Do not create memory_candidates from temporary task state or todos unless the user explicitly asked Pal to remember/save them.\n"
    "- Do not create memory_candidates for repair lessons, procedures, behavior rules, routing advice, or skill workflows unless the user explicitly asked to remember/save them as memory.\n"
    "- If nothing worth extracting, return an empty memory_candidates list.\n"
    "- title, summary, and source_excerpt/search_text serve different purposes: title is a short label; summary is compressed prompt content; source_excerpt/search_text are retrieval/audit terms.\n"
    "The pal compact tracks the user and current collaboration continuity."
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


def build_pal_compaction_source_text(items: list[list[L1TranscriptMessage]], *, target_input_budget: int) -> str:
    previous_summary = current_summary_from_l1(items)
    compactable_turns = compactable_l1_turns(items)
    return render_pal_compaction_source_v2(
        previous_summary=previous_summary,
        turns=compactable_turns,
        limit=max(256, target_input_budget or 0),
    )


def compactable_l1_turns(items: list[list[L1TranscriptMessage]]) -> list[list[L1TranscriptMessage]]:
    turns: list[list[L1TranscriptMessage]] = []
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
            ) == L1MessageKind.RUNTIME_CONTEXT_SUMMARY
            for message in normalized
        ):
            continue
        turns.append(normalized)
    return turns


def render_pal_compaction_source_v2(
    *,
    previous_summary: L2Entry | None,
    turns: list[list[L1TranscriptMessage]],
    limit: int,
) -> str:
    hot_turns = turns[-PAL_COMPACT_HOT_RAW_TURNS:] if PAL_COMPACT_HOT_RAW_TURNS > 0 else []
    warm_end = max(len(turns) - len(hot_turns), 0)
    warm_start = max(warm_end - PAL_COMPACT_WARM_TURNS, 0)
    warm_turns = turns[warm_start:warm_end]
    retired_count = max(warm_start, 0)

    prefix = _render_pal_compaction_source_prefix(previous_summary=previous_summary, retired_count=retired_count)
    hot_section = _render_turn_window("Hot Raw Turns", hot_turns, raw=True)
    warm_section = _render_turn_window("Warm Turns To Compress", warm_turns, raw=False)
    source = _join_sections(prefix, warm_section, hot_section, "</compact_source>")
    while len(source) > limit and warm_turns:
        warm_turns = warm_turns[1:]
        retired_count += 1
        prefix = _render_pal_compaction_source_prefix(previous_summary=previous_summary, retired_count=retired_count)
        warm_section = _render_turn_window("Warm Turns To Compress", warm_turns, raw=False)
        source = _join_sections(prefix, warm_section, hot_section, "</compact_source>")
    return source.strip()


def render_compact_context_for_llm(*, summary: str, payload: dict[str, object]) -> str:
    """Render structured compact payloads as XML-wrapped Markdown."""

    if not is_compaction_payload(payload):
        return "<conversation_summary>\n" + str(summary or "").strip() + "\n</conversation_summary>"
    return render_pal_compact_context_for_llm(summary=summary, payload=payload)


def render_pal_compact_context_for_llm(*, summary: str, payload: dict[str, object]) -> str:
    return render_pal_v2_compact_context_for_llm(summary=summary, payload=payload)


def render_pal_v2_compact_context_for_llm(*, summary: str, payload: dict[str, object]) -> str:
    continuity = payload.get("continuity") if isinstance(payload.get("continuity"), dict) else {}
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
        ("Retired Or Superseded Context", "retired_or_superseded_context"),
        ("Optional Next Step", "optional_next_step"),
    )
    rendered = _render_compact_sections(fields=sections, continuity=dict(continuity))
    if rendered:
        lines.append(rendered)
    summary_text = str(summary or "").strip()
    if summary_text:
        lines.extend(["", "### Summary", summary_text])
    candidates = payload.get("memory_candidates")
    if isinstance(candidates, list) and candidates:
        lines.extend(["", "### Durable Memory Candidates Pending Approval"])
        for item in candidates[:8]:
            if isinstance(item, dict):
                title_text = str(item.get("title") or item.get("summary") or "candidate").strip()
                kind_text = str(item.get("kind") or "candidate").strip()
                lines.append(f"- {kind_text}: {title_text}")
    lines.append("</compact_context>")
    return "\n".join(lines).strip()


def is_compaction_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("schema") or "").strip() in COMPACTION_SCHEMAS


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


def coerce_structured_compaction_payload(
    raw: Any,
    *,
    fallback_summary: str,
    existing_summary: L2Entry | None,
    profile: CompactionProfile = CompactionProfile.PAL,
) -> dict[str, Any]:
    if profile != CompactionProfile.PAL:
        raise ValueError("MemoryService compact handles Pal context only; use the minion compact adapter for minion context")
    payload = raw if isinstance(raw, dict) else {}
    schema = str(payload.get("schema") or "").strip()
    if schema == COMPACTION_SCHEMA_PAL_V2:
        return _coerce_current_structured_compaction_payload(
            payload,
            fallback_summary=fallback_summary,
            existing_summary=existing_summary,
            profile=profile,
        )
    if not payload and fallback_summary:
        return _coerce_current_structured_compaction_payload(
            {
                "schema": COMPACTION_SCHEMA_PAL_V2,
                "kind": "pal",
                "continuity": {},
                "summary": {
                    "summary": fallback_summary,
                    "search_text": fallback_summary,
                },
            },
            fallback_summary=fallback_summary,
            existing_summary=existing_summary,
            profile=profile,
        )
    if payload:
        raise ValueError("structured compaction payload missing recognized schema")
    raise ValueError("compact summary is empty")


def _coerce_current_structured_compaction_payload(
    payload: dict[str, Any],
    *,
    fallback_summary: str,
    existing_summary: L2Entry | None,
    profile: CompactionProfile = CompactionProfile.PAL,
) -> dict[str, Any]:
    _ = existing_summary
    if profile != CompactionProfile.PAL:
        raise ValueError("MemoryService compact handles Pal context only; use the minion compact adapter for minion context")
    return _coerce_pal_compaction_payload(payload, fallback_summary=fallback_summary)


def _coerce_pal_compaction_payload(payload: dict[str, Any], *, fallback_summary: str) -> dict[str, Any]:
    summary_payload = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    continuity = payload.get("continuity") if isinstance(payload.get("continuity"), dict) else {}
    memory_candidates = coerce_memory_candidate_list(payload.get("memory_candidates"))
    summary_text = str(summary_payload.get("summary") or "").strip()
    if not summary_text and fallback_summary:
        summary_text = fallback_summary
    if not summary_text:
        raise ValueError("pal compact summary is empty")
    search_text = str(summary_payload.get("search_text") or "").strip() or summary_text
    summary_payload_blob = {
        "schema": COMPACTION_SCHEMA_PAL_V2,
        "kind": "pal",
        "continuity": _normalize_pal_v2_continuity(continuity),
        "summary": {
            "summary": summary_text,
            "search_text": search_text,
        },
        "memory_candidates": memory_candidates,
    }
    rendered = render_compact_context_for_llm(summary=summary_text, payload=summary_payload_blob)
    summary_entry = _make_summary_entry(
        summary=summary_text,
        rendered=rendered,
        search_text=search_text,
        payload=summary_payload_blob,
    )
    return {"summary_entry": summary_entry, "stable_entries": []}


def _make_summary_entry(
    *,
    summary: str,
    rendered: str,
    search_text: str,
    payload: dict[str, Any],
    title: str = SUMMARY_TITLE,
) -> L2Entry:
    summary_text = str(summary or "").strip()
    rendered_text = str(rendered or "").strip() or summary_text
    search = str(search_text or "").strip() or summary_text
    return L2Entry(
        entry_id=SUMMARY_ENTRY_ID,
        kind="summary",
        scope="system",
        title=str(title or "").strip() or SUMMARY_TITLE,
        summary=summary_text,
        source_kind="l1_compaction",
        candidate_state="stable",
        touched_at=utc_now(),
        rendered=rendered_text,
        search_text=search,
        payload=dict(payload or {}),
    )


def _normalize_pal_v2_continuity(continuity: dict[str, Any]) -> dict[str, Any]:
    source = dict(continuity or {})
    return {
        "current_focus": _first_present(source, "current_focus"),
        "primary_request_and_intent": _first_present(source, "primary_request_and_intent"),
        "active_operating_instructions": _listish(source.get("active_operating_instructions")),
        "active_requests": _listish(source.get("active_requests")),
        "temporary_task_state": _listish(source.get("temporary_task_state")),
        "key_decisions": _listish(source.get("key_decisions")),
        "pending_questions": _listish(source.get("pending_questions")),
        "recent_raw_turns": _listish(source.get("recent_raw_turns")),
        "warm_compressed_turns": _listish(source.get("warm_compressed_turns")),
        "retired_or_superseded_context": _listish(source.get("retired_or_superseded_context")),
        "optional_next_step": _first_present(source, "optional_next_step"),
    }


def _render_pal_compaction_source_prefix(*, previous_summary: L2Entry | None, retired_count: int) -> str:
    lines = [
        '<compact_source kind="pal" schema_target="pal.compaction.pal.v2">',
        "## Source Rules",
        "- This source is mechanically assembled from committed Pal turns.",
        "- Runtime context summaries are not transcript turns and must not be summarized as user/assistant history.",
        "- Previous compact data is already lossy; use it only as a seed for still-active state that recent turns do not contradict.",
        "- Task state is temporary. Completed, cancelled, superseded, or user-rejected items belong in retired context.",
        "",
        "## Previous Compact Seed",
    ]
    seed = _render_previous_compact_seed(previous_summary)
    lines.append(seed or "No previous compact seed.")
    lines.extend([
        "",
        "## Retired Boundary",
        f"Committed turns before the warm window: {retired_count}. They are retired by default unless the previous seed carries still-active state.",
    ])
    return "\n".join(lines).strip()


def _render_previous_compact_seed(entry: L2Entry | None) -> str:
    if entry is None:
        return ""
    payload = dict(entry.payload or {})
    schema = str(payload.get("schema") or "").strip()
    if schema == COMPACTION_SCHEMA_PAL_V2:
        continuity = payload.get("continuity") if isinstance(payload.get("continuity"), dict) else {}
        fields = (
            ("Current Focus", "current_focus"),
            ("Active Operating Instructions", "active_operating_instructions"),
            ("Active Requests", "active_requests"),
            ("Temporary Task State", "temporary_task_state"),
            ("Key Decisions", "key_decisions"),
            ("Pending Questions", "pending_questions"),
            ("Optional Next Step", "optional_next_step"),
        )
        sections: list[str] = []
        for title, key in fields:
            rendered = _render_source_markdown_value(continuity.get(key) if isinstance(continuity, dict) else None)
            if rendered:
                sections.extend([f"### {title}", rendered])
        return "\n\n".join(sections).strip() or "Previous compact had v2 payload but no active seed fields."
    if schema:
        return "Legacy compact payload exists but is not pal.compaction.pal.v2; recent committed turns take priority."
    if str(entry.rendered or entry.summary or "").strip():
        return "Legacy compact text exists without structured payload; do not treat it as transcript or active truth."
    return ""


def _render_turn_window(title: str, turns: list[list[L1TranscriptMessage]], *, raw: bool) -> str:
    lines = [f"## {title}"]
    if not turns:
        lines.append("None.")
        return "\n".join(lines)
    total = len(turns)
    for index, turn in enumerate(turns, start=1):
        label = f"turn {index - total}" if raw else f"turn {index}"
        lines.extend([f"### {label}", _render_l1_turn_for_source(turn)])
    return "\n".join(lines).strip()


def _render_l1_turn_for_source(messages: list[L1TranscriptMessage]) -> str:
    lines: list[str] = []
    for message in messages:
        role = str(message.role or "").strip()
        content = str(message.content or "").strip()
        kind = normalize_l1_message_kind(
            message.kind,
            role=role,
            tool_calls=message.tool_calls,
            tool_call_id=message.tool_call_id,
        )
        if kind not in _COMPACTABLE_L1_MESSAGE_KINDS:
            continue
        if role == "assistant":
            content = strip_persistent_system_reminders(content)
        if role and content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines).strip() or "No compactable text in this turn."


def _render_compact_sections(*, fields: tuple[tuple[str, str], ...], continuity: dict[str, object]) -> str:
    sections: list[str] = []
    for title, key in fields:
        value = continuity.get(key)
        rendered = _render_markdown_value(value)
        if rendered:
            sections.extend([f"### {title}", rendered])
    return "\n\n".join(sections).strip()


def _render_source_markdown_value(value: object) -> str:
    if value in (None, "", []):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            rendered = _render_source_markdown_value(item)
            if rendered:
                parts.append(f"- {key}: {rendered}")
        return "\n".join(parts)
    if isinstance(value, (list, tuple)):
        lines = []
        for item in value:
            rendered = _render_source_markdown_value(item)
            if rendered:
                lines.append(f"- {rendered}" if "\n" not in rendered else rendered)
        return "\n".join(lines)
    return str(value).strip()


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
    if isinstance(value, list):
        return [item for item in value if item not in (None, "")]
    if isinstance(value, tuple):
        return [item for item in value if item not in (None, "")]
    return [value]


def _join_sections(*sections: str) -> str:
    return "\n\n".join(str(section or "").strip() for section in sections if str(section or "").strip())

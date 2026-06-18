from __future__ import annotations

from pal.memory.contracts import L3MutationResult, L3RecallResult, L3RecallView, MemoryQuery

COMPACTION_SCHEMA_PAL_V1 = "pal.compaction.pal.v1"
COMPACTION_SCHEMA_MINION_V1 = "pal.compaction.minion.v1"
COMPACTION_SCHEMA_V2 = "pal.compaction.v2"
COMPACTION_SCHEMAS = {COMPACTION_SCHEMA_PAL_V1, COMPACTION_SCHEMA_MINION_V1, COMPACTION_SCHEMA_V2}
_MAX_RECALL_ITEMS = 3
_MAX_RECALL_LINE_CHARS = 240


def normalize_recall_view(raw: object) -> L3RecallView:
    if isinstance(raw, L3RecallView):
        return raw
    value = str(raw or "").strip().lower()
    return L3RecallView.ORIGIN if value == L3RecallView.ORIGIN.value else L3RecallView.SUMMARY


def build_recall_structured_payload(
    *,
    provider_id: str,
    query: MemoryQuery,
    result: L3RecallResult,
    view: L3RecallView | str,
) -> dict[str, object]:
    normalized_view = normalize_recall_view(view)
    metadata = dict(result.metadata or {})
    payload: dict[str, object] = {
        "provider_id": str(provider_id or "").strip() or "unknown",
        "view": normalized_view.value,
        "queries": list(query.queries),
        "topic_scope": list(query.topic_scope),
        "hit_count": len(list(result.hits or [])),
        "hits_preview": _render_hit_previews(list(result.hits or []), view=normalized_view),
    }
    minimal_metadata = {
        "retrieval_mode": metadata.get("retrieval_mode"),
        "degraded": metadata.get("degraded"),
        "degraded_reason": metadata.get("degraded_reason"),
    }
    minimal_metadata = {key: value for key, value in minimal_metadata.items() if value not in (None, "", False)}
    if minimal_metadata:
        payload["metadata"] = minimal_metadata
    return payload


def render_recall_result_for_llm(
    *,
    provider_id: str,
    query: MemoryQuery,
    result: L3RecallResult,
    view: L3RecallView | str,
) -> str:
    _ = (provider_id, query)
    normalized_view = normalize_recall_view(view)
    lines: list[str] = [f'<recalled_memories view="{normalized_view.value}">']

    item_lines = _render_hit_lines(list(result.hits or []), view=normalized_view)
    if item_lines:
        lines.extend(item_lines)
    else:
        lines.append("No matching memories found.")
    lines.append("</recalled_memories>")
    return "\n".join(lines)


def build_mutation_structured_payload(result: L3MutationResult) -> dict[str, object]:
    payload = dict(result.hit or {})
    mem_ref = str(result.document_id or payload.get("mem_ref") or payload.get("document_id") or "").strip()
    payload.pop("document_id", None)
    if mem_ref:
        payload["mem_ref"] = mem_ref
    if result.metadata:
        payload["metadata"] = dict(result.metadata)
    if not payload:
        payload = {"mem_ref": mem_ref} if mem_ref else {}
    return payload


def render_mutation_result_for_llm(action: str, result: L3MutationResult) -> str:
    mem_ref = str(result.document_id or "").strip()
    lines = [f"Memory {action} result:", f"status: {result.status}"]
    if mem_ref:
        lines.append(f"mem_ref: {mem_ref}")
    return "\n".join(lines)


def render_compact_context_for_llm(*, summary: str, payload: dict[str, object]) -> str:
    """Render structured compact payloads as XML-wrapped Markdown."""

    if not is_compaction_payload(payload):
        return "<conversation_summary>\n" + str(summary or "").strip() + "\n</conversation_summary>"
    if compact_payload_kind(payload) == "minion":
        return render_minion_compact_context_for_llm(summary=summary, payload=payload)
    return render_pal_compact_context_for_llm(summary=summary, payload=payload)


def render_pal_compact_context_for_llm(*, summary: str, payload: dict[str, object]) -> str:
    continuity = payload.get("continuity") if isinstance(payload.get("continuity"), dict) else {}
    lines = [
        '<compact_context kind="pal" authority="conversation_continuity">',
        "## Conversation Continuity",
        "",
        "This is compressed prior conversation context, not a new user request.",
        "Use it to recover the user's intent, constraints, and current collaboration thread.",
        "",
    ]
    rendered = _render_pal_continuity(dict(continuity))
    if rendered:
        lines.append(rendered)
    summary_text = str(summary or "").strip()
    if summary_text:
        lines.extend(["", "### Summary", summary_text])
    candidates = payload.get("memory_candidates")
    if isinstance(candidates, list) and candidates:
        lines.extend(["", "### Memory Candidates"])
        for item in candidates[:8]:
            if isinstance(item, dict):
                title_text = str(item.get("title") or item.get("summary") or "candidate").strip()
                kind_text = str(item.get("kind") or "candidate").strip()
                lines.append(f"- {kind_text}: {title_text}")
    lines.append("</compact_context>")
    return "\n".join(lines).strip()


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


def is_compaction_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("schema") or "").strip() in COMPACTION_SCHEMAS


def is_compaction_v2_payload(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    return str(payload.get("schema") or "").strip() == COMPACTION_SCHEMA_V2


def compact_payload_kind(payload: object) -> str:
    if not isinstance(payload, dict):
        return "pal"
    schema = str(payload.get("schema") or "").strip()
    if schema == COMPACTION_SCHEMA_MINION_V1:
        return "minion"
    if schema == COMPACTION_SCHEMA_PAL_V1:
        return "pal"
    return normalize_compaction_kind(payload.get("kind"))


def normalize_compaction_kind(value: object) -> str:
    raw = str(value or "").strip().lower()
    return "minion" if raw == "minion" else "pal"


def _render_hit_lines(hits: list[dict[str, object]], *, view: L3RecallView) -> list[str]:
    lines: list[str] = []
    for hit in hits[:_MAX_RECALL_ITEMS]:
        mem_ref = str(hit.get("mem_ref") or hit.get("document_id") or "").strip()
        body = _content_for_view(hit, view=view)
        if not mem_ref or not body:
            continue
        lines.append(f"[{mem_ref}]: {body}")
    return lines


def _render_hit_previews(hits: list[dict[str, object]], *, view: L3RecallView) -> list[dict[str, str]]:
    previews: list[dict[str, str]] = []
    content_key = "search_text" if view == L3RecallView.ORIGIN else "summary"
    for hit in hits[:_MAX_RECALL_ITEMS]:
        mem_ref = str(hit.get("mem_ref") or hit.get("document_id") or "").strip()
        content = _content_for_view(hit, view=view)
        preview = {
            "mem_ref": mem_ref,
            content_key: content,
        }
        previews.append({key: value for key, value in preview.items() if value})
    return previews


def _content_for_view(hit: dict[str, object], *, view: L3RecallView) -> str:
    if view == L3RecallView.ORIGIN:
        raw = hit.get("search_text") or hit.get("rendered") or hit.get("summary") or ""
    else:
        raw = hit.get("summary") or hit.get("rendered") or hit.get("search_text") or ""
    return _clip(str(raw or "").strip(), limit=_MAX_RECALL_LINE_CHARS)


def _clip(text: str, *, limit: int) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 3, 0)].rstrip() + "..."


def _render_pal_continuity(continuity: dict[str, object]) -> str:
    fields = (
        ("Latest User Intent", "latest_user_intent"),
        ("Active Thread", "active_thread"),
        ("Explicit Constraints", "explicit_constraints"),
        ("Decisions Made", "decisions_made"),
        ("Pending Questions", "pending_questions"),
        ("Recent User Delegated Tasks", "recent_user_delegated_tasks"),
        ("Important Refs", "important_refs"),
        ("Stale Or Discarded Context", "stale_or_discarded_context"),
        ("Next Best Action", "next_best_action"),
    )
    return _render_compact_sections(fields=fields, continuity=continuity)


def _render_minion_continuity(continuity: dict[str, object]) -> str:
    fields = (
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

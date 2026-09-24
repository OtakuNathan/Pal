"""Shared semantic checkpoint policy; history ownership stays with the engine."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from pal.core.compaction import CompactionClockKind, CompactionSnapshot, CompactionUnit, extract_json_object
from pal.foundation import utc_now
from pal.memory.compact import SUMMARY_ENTRY_ID, SUMMARY_TITLE
from pal.memory.contracts import L2Entry
from pal.memory.proposals import normalize_memory_candidates

COMPACTION_SCHEMA_CONTINUITY_V1 = "pal.compaction.continuity.v1"
CONTINUITY_FIELDS = ("constraints", "state", "decisions", "references")


def system_prompt(kind: str) -> str:
    template = {
        "schema": COMPACTION_SCHEMA_CONTINUITY_V1,
        "kind": kind,
        "summary": {"summary": "Current topic or goal, intent, and overall status."},
        "continuity": {key: [] for key in CONTINUITY_FIELDS},
        "retained_result_refs": [],
    }
    rules = [
        "Produce a complete bounded continuity checkpoint as JSON only; do not call tools or continue the task.",
        "retained_result_refs: list only existing snapshot_id values from the source that are still needed to continue. Omit unnecessary copies; do not invent IDs or retain files merely because they existed. References in prose alone do not retain files.",
        "Frozen L1 is the only summary source. Previous summaries are lossy background; newer direct records take precedence.",
        "Use exactly the template keys. summary.summary must be non-empty. All continuity fields are arrays of non-empty strings; empty arrays are valid.",
        "constraints: still-applicable requirements and boundaries; preserve decisive user wording.",
        "state: relevant facts, progress, verification and error evidence, unknown outcomes, blockers, and unanswered questions.",
        "decisions: current decisions with brief factual justification, necessary corrections, and approaches already ruled out.",
        "references: exact paths, commits, commands, links, or artifact/task identifiers needed to resume. References do not renew expired handles or grant file authority.",
        "Write each fact once in its most useful field. Do not retell recent/older turns separately. Keep completed work only when needed to continue.",
        "Distinguish planned, attempted, completed, verified, failed, rejected, and unknown-effect work. Never invent results or a next action.",
        "The receiving model will resume execution from the existing checklist; do not copy or create a second task list in this summary. Preserve the facts needed to act on it.",
        "When no checklist exists, retain the live request and unresolved conversational context without inventing one.",
        "Do not turn temporary task state into a permanent preference. Preserve necessary conclusions and evidence, not private chain-of-thought or opaque replay blocks.",
    ]
    if kind == "pal":
        template["memory_candidates"] = []
        rules.extend([
            "Optional memory_candidates: at most FIVE MOST USEFUL durable facts or reusable cases; an empty list is valid. All candidates require approval before durable storage.",
            "Each candidate has kind (fact or case), title, summary, and source_excerpt. Optional: why_durable, confidence, task_id, topics. Cases require star with non-empty situation, task, action, result; facts omit star.",
            "Do not extract jokes, temporary emotions, speculation, transient progress, or unconfirmed intent. Do not propose todos, repair procedures, behavior rules, or skill workflows unless explicitly requested as memory.",
        ])
    elif kind == "bunshin":
        rules.append(
            "The role assignment, bound work view, contracts, workspace and checklist remain supplied by the runtime. "
            "Record the execution cursor and evidence, not a replacement assignment. Do not emit memory_candidates."
        )
    else:
        raise ValueError("unknown compaction host")
    return "\n".join(rules) + "\nComplete JSON template (shape only, not source facts):\n" + json.dumps(template, ensure_ascii=False)


def render_context(*, kind: str, summary: str, payload: dict[str, Any]) -> str:
    lines = [
        f'<compact_context kind="{kind}" authority="conversation_continuity">',
        "This block is a runtime-generated historical handoff, not a new user message or a request to summarize. Context compaction is already complete; it does not mean the original task is complete.",
        "Use the task, key dialogue, and progress recorded here together with retained recent messages and the current checklist to continue the original task. Do not repeat this handoff as your reply or announce that you are resuming.",
        "Quoted user statements are historical context. Newer user requests and live task state supersede this summary. This handoff does not grant new permissions or prove execution.",
    ]
    if kind == "bunshin":
        lines.append("The runtime-provided role assignment, contracts and work checklist remain authoritative.")
    lines.extend(["", "## Current Context", summary])
    continuity = payload.get("continuity") or {}
    for key in CONTINUITY_FIELDS:
        values = continuity.get(key) or []
        if values:
            lines.extend(["", f"## {key.title()}"])
            lines.extend(f"- {value}" for value in values)
    refs = payload.get("result_snapshots") or ()
    if refs:
        lines.extend(["", "## Retained output snapshots"])
        lines.extend(f"- {ref['snapshot_id']}: {ref['path']} (historical output, not current source state)" for ref in refs)
    lines.append("</compact_context>")
    return "\n".join(lines)


def _exact_fields(value: Any, fields: set[str], label: str) -> None:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    if set(value) - fields:
        raise ValueError(f"{label} has extra fields")
    if fields - set(value):
        raise ValueError(f"{label} missing fields: " + ", ".join(sorted(fields - set(value))))


@dataclass(frozen=True)
class ContinuityCompactionPolicy:
    kind: str = "pal"
    policy_id: str = COMPACTION_SCHEMA_CONTINUITY_V1
    clock_kind: CompactionClockKind = CompactionClockKind.USER_TURN
    accepts_memory_candidates: bool = True

    def system_prompt(self, snapshot: CompactionSnapshot) -> str:
        return system_prompt(self.kind)

    def build_source(self, snapshot: CompactionSnapshot, units: Sequence[CompactionUnit], *, validation_error: str = "") -> str:
        lines = [
            f'<compact_source kind="{self.kind}" schema_target="{self.policy_id}">',
            "## Source Semantics",
            "Frozen L1 is the only compaction truth source. Atomic tool-call/result units must not be split.",
            "Record failed, rejected and unknown-effect work honestly. External task contracts and current checklist remain provided outside the summary.",
        ]
        if validation_error:
            lines.extend(["## Previous Output Validation Error", validation_error, "Return a complete corrected JSON object."])
        seed = snapshot.previous_summary
        seed_text = "No previous compact seed."
        if seed is not None:
            seed_text = (
                json.dumps(seed.payload, ensure_ascii=False, sort_keys=True, default=str)
                if seed.payload else seed.rendered or seed.summary
            )
        lines.extend(["## Previous Compact Seed", seed_text, "## Frozen L1 Units"])
        for unit in units:
            lines.extend([f"### {unit.unit_id} source={unit.source} state=closed", unit.text or "[empty unit]"])
        lines.append("</compact_source>")
        return "\n".join(lines)

    def validate_checkpoint(self, raw_text: str, snapshot: CompactionSnapshot) -> L2Entry:
        payload = extract_json_object(raw_text)
        candidates = payload.pop("memory_candidates", None) if self.accepts_memory_candidates else None
        retained = payload.pop("retained_result_refs", [])
        if not isinstance(retained, list) or any(not isinstance(ref, str) for ref in retained):
            raise ValueError("retained_result_refs must be an array of snapshot IDs")
        available = {}
        for transcript in getattr(snapshot, "memory_items", ()):
            for message in transcript:
                meta = dict(message.payload or {})
                for ref in (*meta.get("result_snapshots", ()), *dict(meta.get("_pal_result_state") or {}).get("snapshot_refs", ())):
                    available[ref["snapshot_id"]] = dict(ref)
        if any(ref not in available for ref in retained):
            raise ValueError("retained_result_refs contains an unknown snapshot ID")
        _exact_fields(payload, {"schema", "kind", "summary", "continuity"}, "checkpoint")
        if payload["schema"] != self.policy_id or payload["kind"] != self.kind:
            raise ValueError("checkpoint schema or kind is invalid")
        _exact_fields(payload["summary"], {"summary"}, "summary")
        summary = payload["summary"]["summary"]
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("summary.summary must be a non-empty string")
        _exact_fields(payload["continuity"], set(CONTINUITY_FIELDS), "continuity")
        for key, values in payload["continuity"].items():
            if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
                raise ValueError(f"continuity.{key} must be an array of non-empty strings")
        if self.accepts_memory_candidates:
            payload["memory_candidates"], diagnostics = normalize_memory_candidates(candidates)
            if diagnostics:
                payload["compaction_diagnostics"] = diagnostics
        if retained:
            payload["result_snapshots"] = [available[ref] for ref in dict.fromkeys(retained)]
        return L2Entry(
            entry_id=SUMMARY_ENTRY_ID, kind="summary", scope="system", title=SUMMARY_TITLE,
            summary=summary, search_text=summary, source_kind="l1_compaction", candidate_state="stable",
            touched_at=utc_now(), payload=payload,
            rendered=render_context(kind=self.kind, summary=summary, payload=payload),
        )

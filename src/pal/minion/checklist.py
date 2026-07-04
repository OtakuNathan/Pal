from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pal.foundation import utc_now


@dataclass(frozen=True)
class ChecklistItem:
    item_id: str
    kind: str
    source_text: str
    status: str = "pending"
    source_kind: str = ""
    source_ref: str = ""
    parent_item_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "id": self.item_id,
                "kind": self.kind,
                "status": self.status,
                "source_text": self.source_text,
                "source_kind": self.source_kind,
                "source_ref": self.source_ref,
                "parent_item_id": self.parent_item_id,
                "metadata": dict(self.metadata),
            }.items()
            if value not in ("", {}, [], None)
        }


def build_acceptance_checklist(*sources: Any, status: str = "pending") -> list[dict[str, Any]]:
    items: list[ChecklistItem] = []
    seen: set[str] = set()
    for source in sources:
        for text in string_list(source):
            token = loose_token(text)
            if not token or token in seen:
                continue
            seen.add(token)
            items.append(
                ChecklistItem(
                    item_id=f"AC-{len(items) + 1}",
                    kind="acceptance",
                    source_text=text,
                    status=status,
                    source_kind="acceptance_criteria",
                )
            )
    return [item.to_dict() for item in items]


def build_requirements_checklist(*, goal: str = "", requirements_brief: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    brief = dict(requirements_brief or {})
    raw_items: list[dict[str, str]] = []

    explicit_source_keys = (
        "requirements_checklist",
        "requirement_checklist",
        "requirement_ledger",
        "requirements",
        "hard_requirements",
        "source_items",
        "source_requirements",
        "source_acceptance",
        "source_ledger",
        "user_requirements",
    )
    for key in explicit_source_keys:
        raw_items.extend(_requirement_items_from_value(brief.get(key), source_kind=f"requirements_brief.{key}", whole_string=True))

    if not raw_items:
        raw_items.extend(
            _requirement_items_from_value(
                brief.get("acceptance_criteria"),
                source_kind="requirements_brief.acceptance_criteria",
                whole_string=True,
            )
        )

    if not raw_items:
        for key in ("summary", "scope", "instruction", "instructions", "text", "raw", "description", "goal"):
            raw_items.extend(_requirement_items_from_value(brief.get(key), source_kind=f"requirements_brief.{key}", whole_string=False))

    if not raw_items:
        raw_items.extend(_requirement_items_from_value(goal, source_kind="goal", whole_string=False))

    items: list[ChecklistItem] = []
    seen: set[str] = set()
    for raw in raw_items:
        text = str(raw.get("text") or "").strip()
        token = loose_token(text)
        if not token or token in seen:
            continue
        seen.add(token)
        source_ref = str(raw.get("source_ref") or raw.get("source_kind") or "requirement").strip()
        items.append(
            ChecklistItem(
                item_id=f"REQ-{len(items) + 1:03d}",
                kind="requirement",
                source_text=text,
                status="pending",
                source_kind=str(raw.get("source_kind") or "requirements_brief").strip(),
                source_ref=source_ref,
                metadata={"priority": "hard"},
            )
        )
    return [item.to_dict() for item in items]


def requirements_checklist_to_gate_contract(checklist: list[dict[str, Any]]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for item in checklist:
        if not isinstance(item, dict):
            continue
        text = _item_text(item)
        if not text:
            continue
        item_id = str(item.get("id") or f"REQ-{len(checks) + 1:03d}").strip()
        checks.append(
            {
                "claim": text,
                "priority": "hard",
                "kind": "semantic",
                "source_ref": item_id,
                "rationale": "Compiled mechanically from Pal's prepared requirements checklist.",
            }
        )
    return {"checks": checks} if checks else {}


def build_evidence_projection(refs: Any) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for raw in list(refs or []):
        if not isinstance(raw, dict):
            continue
        ref = dict(raw)
        item: dict[str, Any] = {
            "id": f"EV-{len(projected) + 1}",
            "kind": str(ref.get("kind") or "").strip(),
            "status": ref.get("status"),
            "ok": ref.get("ok"),
            "summary": _first_text(ref, "summary", "command", "path", "query"),
        }
        for key in (
            "evidence_ref_id",
            "call_id",
            "ledger_id",
            "tool_name",
            "operation",
            "command",
            "cwd",
            "exit_code",
            "path",
            "query",
            "evidence_id",
            "method",
            "server_id",
            "workspace_root",
            "file",
            "file_sha256",
            "freshness",
            "unavailable_reason",
            "stdout_preview",
            "stderr_preview",
        ):
            if ref.get(key) not in (None, "", []):
                item[key] = ref.get(key)
        projected.append({key: value for key, value in item.items() if value not in (None, "", [])})
    return projected


def resolve_acceptance_ref(value: Any, checklist: list[dict[str, Any]]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.lower().replace("_", "-")
    for item in checklist:
        item_id = str(item.get("id") or "").strip()
        if item_id and normalized == item_id.lower():
            return item_id
    compact = normalized.replace("-", "").lstrip("#")
    if compact.isdigit():
        return _acceptance_id_at(checklist, int(compact))
    if compact.startswith("ac") and compact[2:].isdigit():
        return _acceptance_id_at(checklist, int(compact[2:]))
    loose = loose_token(text)
    for item in checklist:
        if loose and loose == loose_token(item.get("source_text")):
            return str(item.get("id") or "").strip()
    return text


def normalize_coverage_refs(value: Any, checklist: list[dict[str, Any]], *, legacy_index: bool = False) -> Any:
    if isinstance(value, (list, tuple, set)):
        return [normalize_coverage_refs(item, checklist, legacy_index=legacy_index) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_coverage_refs(nested, checklist, legacy_index=legacy_index) for key, nested in value.items()}
    resolved = resolve_acceptance_ref(value, checklist)
    if legacy_index and resolved.upper().startswith("AC-"):
        return resolved.split("-", 1)[1]
    return resolved


def resolve_evidence_ref(value: Any, projection: list[dict[str, Any]]) -> dict[str, Any]:
    ref = dict(value or {}) if isinstance(value, dict) else {"evidence_id": str(value or "").strip()}
    candidates = [
        str(ref.get("evidence_id") or ref.get("id") or "").strip(),
        str(ref.get("evidence_ref_id") or "").strip(),
        str(ref.get("call_id") or "").strip(),
        str(ref.get("ledger_id") or "").strip(),
    ]
    for item in projection:
        item_candidates = {
            str(item.get("id") or "").strip(),
            str(item.get("evidence_ref_id") or "").strip(),
            str(item.get("call_id") or "").strip(),
            str(item.get("ledger_id") or "").strip(),
        }
        if any(candidate and candidate in item_candidates for candidate in candidates):
            return dict(item)
    return {}


def repair_acceptance_refs(finding: dict[str, Any], checklist: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for key in ("failed_acceptance_criteria", "acceptance_criteria", "covers", "coverage", "acceptance_refs"):
        refs.extend(_flatten_refs(finding.get(key)))
    normalized: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        resolved = resolve_acceptance_ref(ref, checklist)
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        normalized.append(resolved)
    return normalized


def compact_checklist(items: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        compacted.append(
            {
                key: value
                for key, value in {
                    "id": item.get("id"),
                    "kind": item.get("kind"),
                    "status": item.get("status"),
                    "text": item.get("source_text") or item.get("action") or item.get("summary"),
                    "parent_item_id": item.get("parent_item_id"),
                }.items()
                if value not in (None, "", [])
            }
        )
    return compacted


REQUIRED_CHECKLIST_KINDS = frozenset({"acceptance", "repair", "inspect", "implement", "test", "verify"})
OPTIONAL_CHECKLIST_KINDS = frozenset({"checkpoint", "note", "advisory", "optional"})


@dataclass
class MilestoneChecklistLedger:
    state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_workspace(cls, workspace: dict[str, Any]) -> "MilestoneChecklistLedger":
        raw_state = workspace.get("milestone_checklist")
        ledger = cls.from_state(raw_state if isinstance(raw_state, dict) else {})
        if not ledger.state.get("items"):
            ledger = cls.from_sources(workspace)
        workspace["milestone_checklist"] = ledger.state
        return ledger

    @classmethod
    def from_sources(cls, workspace: dict[str, Any]) -> "MilestoneChecklistLedger":
        data = dict(workspace or {})
        prompt_view = _dict(data.get("prompt_view"))
        milestone = _dict(prompt_view.get("milestone"))
        milestone_metadata = _dict(milestone.get("metadata"))
        ledger = cls(_empty_state())

        rich_acceptance = [dict(item) for item in list(milestone_metadata.get("acceptance_checklist") or []) if isinstance(item, dict)]
        if rich_acceptance:
            for item in rich_acceptance:
                ledger.merge_item(_acceptance_item_from_rich(item, source_kind="milestone_acceptance"))
        else:
            for item in build_acceptance_checklist(milestone.get("acceptance_criteria") or milestone.get("acceptance")):
                ledger.merge_item(item)

        for item in list(milestone_metadata.get("implementation_checklist") or []):
            if isinstance(item, dict):
                ledger.merge_item(_step_item_from_raw(item, ledger.state))

        repair_context = _dict(data.get("repair_context"))
        repair_bill = _dict(repair_context.get("repair_bill") or repair_context)
        ledger.merge_repair_bill(repair_bill)

        checkpoint_repair = _dict(data.get("checkpoint_repair"))
        for item in list(checkpoint_repair.get("repair_checklist") or []):
            if isinstance(item, dict):
                ledger.merge_item(_repair_item_from_raw(item, ledger.state, source_kind="checkpoint_repair"))

        active_todo = _dict(data.get("active_gate_todo"))
        for item in list(active_todo.get("repair_items") or active_todo.get("items") or []):
            if isinstance(item, dict):
                text = _first_text(item, "source_text", "action", "summary", "text")
                if str(item.get("kind") or "").strip().lower() == "repair" or text:
                    ledger.merge_item(_repair_item_from_raw(item, ledger.state, source_kind="active_gate_todo"))

        if _workspace_allows_source_requirement_checklist(data):
            requirements_brief = _dict(data.get("requirements_brief"))
            for item in list(requirements_brief.get("requirements_checklist") or []):
                if isinstance(item, dict):
                    ledger.merge_item(_requirement_item_from_raw(item))

        return ledger

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> "MilestoneChecklistLedger":
        raw = state if isinstance(state, dict) else {}
        raw_items = raw.get("items")
        if isinstance(raw_items, dict):
            source_items = []
            for item_id, item in raw_items.items():
                if not isinstance(item, dict):
                    continue
                payload = dict(item)
                payload.setdefault("id", str(item_id))
                source_items.append(payload)
        else:
            source_items = [dict(item) for item in list(raw_items or []) if isinstance(item, dict)]
        ledger = cls(_empty_state())
        for item in source_items:
            ledger.merge_item(item)
        explicit_order = [str(item).strip() for item in list(raw.get("order") or []) if str(item or "").strip()]
        order = [item_id for item_id in explicit_order if item_id in ledger.state["items"]]
        known = set(order)
        for item_id in list(ledger.state["order"]):
            if item_id not in known:
                order.append(item_id)
                known.add(item_id)
        ledger.state["order"] = order
        return ledger

    def merge_item(self, item: dict[str, Any]) -> None:
        normalized = _normalize_checklist_item(item)
        text = _item_text(normalized)
        kind = str(normalized.get("kind") or "step").strip().lower()
        if not text:
            return
        existing_id = self._find_equivalent_item_id(kind, text)
        if existing_id:
            existing = self.state["items"][existing_id]
            existing["metadata"] = {**dict(existing.get("metadata") or {}), **dict(normalized.get("metadata") or {})}
            for key in ("evidence_expectation", "negative_cases", "done_when"):
                if normalized.get(key) not in (None, "", [], {}):
                    existing[key] = normalized.get(key)
            source_kind = str(normalized.get("source_kind") or "").strip()
            if source_kind:
                sources = list(dict(existing.get("metadata") or {}).get("sources") or [])
                source = {"source_kind": source_kind, "source_ref": str(normalized.get("source_ref") or "").strip()}
                if source not in sources:
                    sources.append(source)
                existing.setdefault("metadata", {})["sources"] = sources
            return
        item_id = str(normalized.get("id") or "").strip()
        if not item_id or item_id in self.state["items"]:
            prefix = "AC" if kind == "acceptance" else "REPAIR" if kind == "repair" else "STEP"
            item_id = self.next_item_id(prefix)
        normalized["id"] = item_id
        self.state["items"][item_id] = normalized
        self.state["order"].append(item_id)

    def merge_repair_bill(self, repair_bill: dict[str, Any]) -> None:
        if not repair_bill:
            return
        source_ref = str(repair_bill.get("bill_id") or repair_bill.get("repair_bill_id") or repair_bill.get("kind") or "repair_bill").strip()
        for item in list(repair_bill.get("acceptance_criteria") or []):
            if not isinstance(item, dict):
                continue
            normalized = _normalize_checklist_item(
                {
                    "kind": "repair",
                    "source_text": _first_text(item, "action", "criterion", "source_text", "text", "summary"),
                    "source_kind": "repair_bill",
                    "source_ref": str(item.get("id") or source_ref).strip(),
                    "covers": item.get("covers") or item.get("acceptance_ref") or item.get("acceptance_refs"),
                    "evidence_expectation": item.get("evidence_expectation"),
                    "negative_cases": item.get("negative_cases"),
                    "metadata": {
                        key: item.get(key)
                        for key in ("linked_constraint_refs", "gate_check_refs", "quantifier")
                        if item.get(key) not in (None, "", [], {})
                    },
                }
            )
            metadata = dict(normalized.get("metadata") or {})
            metadata["source_bill_id"] = source_ref
            normalized["metadata"] = metadata
            normalized.pop("id", None)
            self.merge_item(normalized)
        for text in string_list(repair_bill.get("additional_acceptance_criteria")):
            self.merge_item(
                _normalize_checklist_item(
                    {
                        "kind": "repair",
                        "source_text": text,
                        "source_kind": "repair_bill",
                        "source_ref": source_ref,
                        "metadata": {"source_bill_id": source_ref},
                    }
                )
            )

    def items(self, *, scope: str = "all") -> list[dict[str, Any]]:
        requested = str(scope or "all").strip().lower()
        result: list[dict[str, Any]] = []
        for item_id in list(self.state.get("order") or []):
            item = dict((self.state.get("items") or {}).get(item_id) or {})
            if not item:
                continue
            kind = str(item.get("kind") or "").strip().lower()
            if requested in {"", "all"}:
                result.append(item)
            elif requested in {"acceptance", "ac"} and kind == "acceptance":
                result.append(item)
            elif requested in {"requirements", "requirement", "req"} and kind == "requirement":
                result.append(item)
            elif requested in {"steps", "step"} and kind not in {"acceptance", "repair", "requirement"}:
                result.append(item)
            elif requested == "repair" and kind == "repair":
                result.append(item)
        return result

    def mark_done(self, item_id: str, evidence: Any) -> dict[str, Any]:
        item = self.item_or_raise(item_id)
        evidence_items = _evidence_list(evidence)
        if not evidence_items:
            raise ValueError("evidence is required")
        item["status"] = "done"
        item.pop("blocked_reason", None)
        item["evidence"] = _dedupe_evidence([*list(item.get("evidence") or []), *evidence_items])
        item["updated_at"] = utc_now()
        return dict(item)

    def mark_blocked(self, item_id: str, reason: str) -> dict[str, Any]:
        item = self.item_or_raise(item_id)
        blocked_reason = str(reason or "").strip()
        if not blocked_reason:
            raise ValueError("reason is required")
        item["status"] = "blocked"
        item["blocked_reason"] = blocked_reason
        item["updated_at"] = utc_now()
        return dict(item)

    def checkpoint_blockers(self) -> list[dict[str, Any]]:
        blockers: list[dict[str, Any]] = []
        for item in self.items():
            if not _item_required(item):
                continue
            status = str(item.get("status") or "pending").strip().lower()
            item_id = str(item.get("id") or "").strip()
            text = _item_text(item)
            if status == "done":
                if not _evidence_list(item.get("evidence")):
                    blockers.append({"id": item_id, "reason": "done_missing_evidence", "text": text})
                continue
            if status == "blocked":
                blockers.append({"id": item_id, "reason": "item_blocked", "text": text, "blocked_reason": str(item.get("blocked_reason") or "")})
                continue
            blockers.append({"id": item_id, "reason": "item_pending", "text": text})
        return blockers

    def render_for_llm(self, *, scope: str = "all") -> str:
        items = self.items(scope=scope)
        if not items:
            return "No checklist items."
        groups = [
            ("Requirements", [item for item in items if str(item.get("kind") or "") == "requirement"]),
            ("Acceptance gates", [item for item in items if str(item.get("kind") or "") == "acceptance"]),
            ("Repair items", [item for item in items if str(item.get("kind") or "") == "repair"]),
            ("Suggested steps", [item for item in items if str(item.get("kind") or "") not in {"requirement", "acceptance", "repair"}]),
        ]
        lines: list[str] = []
        for title, group_items in groups:
            if not group_items:
                continue
            lines.append(f"{title}:")
            for item in group_items:
                status = str(item.get("status") or "pending").strip()
                item_id = str(item.get("id") or "").strip()
                text = _item_text(item)
                suffix = ""
                if status == "done":
                    suffix = " (evidence recorded)" if _evidence_list(item.get("evidence")) else " (missing evidence)"
                elif status == "blocked" and str(item.get("blocked_reason") or "").strip():
                    suffix = f" (blocked: {str(item.get('blocked_reason')).strip()})"
                metadata = dict(item.get("metadata") or {})
                gate_ref = str(metadata.get("gate_check_ref") or "").strip()
                if gate_ref:
                    suffix = f"{suffix} ({gate_ref})"
                lines.append(f"- {item_id} [{status}]: {text}{suffix}")
        return "\n".join(lines).strip()

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "order": list(self.state.get("order") or []),
            "items": {
                item_id: {key: value for key, value in dict(item).items() if value not in (None, "", [], {})}
                for item_id, item in dict(self.state.get("items") or {}).items()
            },
        }

    def summary(self) -> dict[str, Any]:
        counts = {"pending": 0, "done": 0, "blocked": 0}
        for item in self.items():
            status = str(item.get("status") or "pending").strip().lower()
            counts[status if status in counts else "pending"] += 1
        return {
            "counts": counts,
            "required_blockers": self.checkpoint_blockers(),
            "item_count": len(self.items()),
        }

    def item_or_raise(self, item_id: str) -> dict[str, Any]:
        key = str(item_id or "").strip()
        item = (self.state.get("items") or {}).get(key)
        if not isinstance(item, dict):
            raise KeyError(f"unknown checklist item: {key}")
        return item

    def next_item_id(self, prefix: str) -> str:
        prefix = str(prefix or "ITEM").strip().upper()
        existing = set(str(item) for item in list(self.state.get("order") or []))
        index = 1
        while f"{prefix}-{index}" in existing:
            index += 1
        return f"{prefix}-{index}"

    def _find_equivalent_item_id(self, kind: str, text: str) -> str:
        token = loose_token(text)
        if not token:
            return ""
        for item_id in list(self.state.get("order") or []):
            item = dict((self.state.get("items") or {}).get(item_id) or {})
            if str(item.get("kind") or "").strip().lower() == kind and loose_token(_item_text(item)) == token:
                return item_id
        return ""


def ensure_milestone_checklist_state(workspace: dict[str, Any]) -> dict[str, Any]:
    return MilestoneChecklistLedger.from_workspace(workspace).state


def normalize_milestone_checklist_state(value: Any) -> dict[str, Any]:
    return MilestoneChecklistLedger.from_state(value if isinstance(value, dict) else {}).state


def build_milestone_checklist_state(workspace: dict[str, Any]) -> dict[str, Any]:
    return MilestoneChecklistLedger.from_sources(workspace).state


def checklist_mark_done(state: dict[str, Any], item_id: str, evidence: Any) -> dict[str, Any]:
    ledger = MilestoneChecklistLedger.from_state(state)
    item = ledger.mark_done(item_id, evidence)
    state.clear()
    state.update(ledger.state)
    return item


def checklist_mark_blocked(state: dict[str, Any], item_id: str, reason: str) -> dict[str, Any]:
    ledger = MilestoneChecklistLedger.from_state(state)
    item = ledger.mark_blocked(item_id, reason)
    state.clear()
    state.update(ledger.state)
    return item


def checklist_items(state: dict[str, Any], *, scope: str = "all") -> list[dict[str, Any]]:
    return MilestoneChecklistLedger.from_state(state).items(scope=scope)


def checklist_checkpoint_blockers(state: dict[str, Any]) -> list[dict[str, Any]]:
    return MilestoneChecklistLedger.from_state(state).checkpoint_blockers()


def render_checklist_for_llm(state: dict[str, Any], *, scope: str = "all") -> str:
    return MilestoneChecklistLedger.from_state(state).render_for_llm(scope=scope)


def compact_checklist_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    return MilestoneChecklistLedger.from_state(state).snapshot()


def _empty_state() -> dict[str, Any]:
    return {"version": 1, "order": [], "items": {}}


def _normalize_checklist_item(value: dict[str, Any]) -> dict[str, Any]:
    item = dict(value or {})
    kind = str(item.get("kind") or "step").strip().lower()
    item_id = str(item.get("id") or "").strip()
    text = _item_text(item)
    status = str(item.get("status") or "pending").strip().lower() or "pending"
    normalized = {
        "id": item_id,
        "kind": kind,
        "status": status,
        "source_text": text,
        "source_kind": str(item.get("source_kind") or "").strip(),
        "source_ref": str(item.get("source_ref") or "").strip(),
        "parent_item_id": str(item.get("parent_item_id") or "").strip(),
        "evidence": _evidence_list(item.get("evidence")),
        "blocked_reason": str(item.get("blocked_reason") or item.get("reason") or "").strip(),
        "covers": string_list(item.get("covers") or item.get("acceptance_ref") or item.get("acceptance_refs")),
        "metadata": _dict(item.get("metadata")),
    }
    for key in ("evidence_expectation", "negative_cases", "done_when", "priority"):
        if item.get(key) not in (None, "", [], {}):
            normalized["metadata"][key] = item.get(key)
    if kind in OPTIONAL_CHECKLIST_KINDS:
        normalized["required"] = False
    elif "required" in item:
        normalized["required"] = bool(item.get("required"))
    return {key: val for key, val in normalized.items() if val not in (None, "", [], {})}


def _acceptance_item_from_rich(value: dict[str, Any], *, source_kind: str) -> dict[str, Any]:
    item = dict(value or {})
    return _normalize_checklist_item(
        {
            "id": str(item.get("id") or "").strip(),
            "kind": "acceptance",
            "status": str(item.get("status") or "pending").strip() or "pending",
            "source_text": _first_text(item, "criterion", "source_text", "text", "summary"),
            "source_kind": source_kind,
            "source_ref": str(item.get("source_ref") or item.get("id") or "").strip(),
            "evidence_expectation": item.get("evidence_expectation"),
            "negative_cases": item.get("negative_cases"),
            "metadata": {
                key: item.get(key)
                for key in ("linked_constraint_refs", "gate_check_refs", "quantifier")
                if item.get(key) not in (None, "", [], {})
            },
        }
    )


def _step_item_from_raw(value: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    item = dict(value or {})
    return _normalize_checklist_item(
        {
            "id": _next_item_id(state, "STEP"),
            "kind": str(item.get("kind") or "step").strip().lower() or "step",
            "source_text": _first_text(item, "action", "source_text", "summary", "text"),
            "source_kind": "implementation_checklist",
            "source_ref": str(item.get("id") or "").strip(),
            "done_when": item.get("done_when"),
            "covers": item.get("covers") or item.get("acceptance_ref") or item.get("acceptance_refs"),
            "required": str(item.get("kind") or "").strip().lower() not in OPTIONAL_CHECKLIST_KINDS,
            "metadata": {key: item.get(key) for key in ("area", "verify") if item.get(key) not in (None, "", [], {})},
        }
    )


def _repair_item_from_raw(value: dict[str, Any], state: dict[str, Any], *, source_kind: str) -> dict[str, Any]:
    item = dict(value or {})
    return _normalize_checklist_item(
        {
            "id": _next_item_id(state, "REPAIR"),
            "kind": "repair",
            "status": str(item.get("status") or "pending").strip() or "pending",
            "source_text": _first_text(item, "action", "source_text", "summary", "text"),
            "source_kind": source_kind,
            "source_ref": str(item.get("id") or "").strip(),
            "covers": item.get("covers") or item.get("acceptance_refs") or item.get("failed_acceptance_criteria"),
            "metadata": {
                key: item.get(key)
                for key in ("area", "verify", "finding_id", "required_fix_id")
                if item.get(key) not in (None, "", [], {})
            },
        }
    )


def _requirement_item_from_raw(value: dict[str, Any]) -> dict[str, Any]:
    item = dict(value or {})
    return _normalize_checklist_item(
        {
            "id": str(item.get("id") or "").strip(),
            "kind": "requirement",
            "status": str(item.get("status") or "pending").strip() or "pending",
            "source_text": _first_text(item, "source_text", "requirement", "claim", "text", "summary"),
            "source_kind": str(item.get("source_kind") or "requirements_brief").strip(),
            "source_ref": str(item.get("source_ref") or item.get("id") or "").strip(),
            "required": False,
            "metadata": {
                **_dict(item.get("metadata")),
                **{key: item.get(key) for key in ("gate_check_ref", "priority") if item.get(key) not in (None, "", [], {})},
            },
        }
    )


def _workspace_allows_source_requirement_checklist(workspace: dict[str, Any]) -> bool:
    prompt_view = _dict(_dict(workspace).get("prompt_view"))
    role = str(prompt_view.get("role") or "").strip().lower()
    if role in {"architect", "planner"}:
        return True
    profile = str(_dict(workspace).get("minion_profile") or "").strip().lower()
    return profile.endswith(".architect") or profile == "architect"


def _merge_checklist_item(state: dict[str, Any], item: dict[str, Any]) -> None:
    normalized = _normalize_checklist_item(item)
    text = _item_text(normalized)
    kind = str(normalized.get("kind") or "step").strip().lower()
    if not text:
        return
    existing_id = _find_equivalent_item_id(state, kind, text)
    if existing_id:
        existing = state["items"][existing_id]
        existing["metadata"] = {**dict(existing.get("metadata") or {}), **dict(normalized.get("metadata") or {})}
        for key in ("evidence_expectation", "negative_cases", "done_when"):
            if normalized.get(key) not in (None, "", [], {}):
                existing[key] = normalized.get(key)
        source_kind = str(normalized.get("source_kind") or "").strip()
        if source_kind:
            sources = list(dict(existing.get("metadata") or {}).get("sources") or [])
            source = {"source_kind": source_kind, "source_ref": str(normalized.get("source_ref") or "").strip()}
            if source not in sources:
                sources.append(source)
            existing.setdefault("metadata", {})["sources"] = sources
        return
    item_id = str(normalized.get("id") or "").strip()
    if not item_id or item_id in state["items"]:
        prefix = "AC" if kind == "acceptance" else "REPAIR" if kind == "repair" else "STEP"
        item_id = _next_item_id(state, prefix)
    normalized["id"] = item_id
    state["items"][item_id] = normalized
    state["order"].append(item_id)


def _find_equivalent_item_id(state: dict[str, Any], kind: str, text: str) -> str:
    token = loose_token(text)
    if not token:
        return ""
    for item_id in list(state.get("order") or []):
        item = dict((state.get("items") or {}).get(item_id) or {})
        if str(item.get("kind") or "").strip().lower() == kind and loose_token(_item_text(item)) == token:
            return item_id
    return ""


def _next_item_id(state: dict[str, Any], prefix: str) -> str:
    prefix = str(prefix or "ITEM").strip().upper()
    existing = set(str(item) for item in list((state or {}).get("order") or []))
    index = 1
    while f"{prefix}-{index}" in existing:
        index += 1
    return f"{prefix}-{index}"


def _item_or_raise(state: dict[str, Any], item_id: str) -> dict[str, Any]:
    normalized = normalize_milestone_checklist_state(state)
    state.clear()
    state.update(normalized)
    key = str(item_id or "").strip()
    item = (state.get("items") or {}).get(key)
    if not isinstance(item, dict):
        raise KeyError(f"unknown checklist item: {key}")
    return item


def _item_required(item: dict[str, Any]) -> bool:
    if "required" in item:
        return bool(item.get("required"))
    kind = str(item.get("kind") or "").strip().lower()
    return kind in REQUIRED_CHECKLIST_KINDS


def _item_text(item: dict[str, Any]) -> str:
    return _first_text(item, "source_text", "criterion", "action", "summary", "text")


def _evidence_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    raw_items = value if isinstance(value, (list, tuple)) else [value]
    result: list[dict[str, Any]] = []
    for raw in raw_items:
        if isinstance(raw, dict):
            item = {str(key): val for key, val in raw.items() if val not in (None, "", [], {})}
        else:
            summary = str(raw or "").strip()
            item = {"summary": summary} if summary else {}
        if item:
            result.append(item)
    return result


def _dedupe_evidence(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = str(sorted(dict(item).items()))
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(item))
    return result


def _dict(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, dict) else {}


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        return [str(item).strip() for item in value.values() if str(item or "").strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


_NUMBERED_OR_BULLET_REQUIREMENT_RE = re.compile(r"^\s*(?:(?:\d{1,3})(?:[.)]|\u3001)\s*|[-*]\s+)(?P<text>\S.*)$")


def _requirement_items_from_value(value: Any, *, source_kind: str, whole_string: bool) -> list[dict[str, str]]:
    if value is None:
        return []
    if isinstance(value, dict):
        direct = _first_text(value, "source_text", "requirement", "claim", "criterion", "text", "summary", "description", "action")
        if direct:
            source_ref = str(value.get("id") or value.get("source_ref") or source_kind).strip()
            return [{"text": direct, "source_kind": source_kind, "source_ref": source_ref}]
        result: list[dict[str, str]] = []
        for key, nested in value.items():
            result.extend(_requirement_items_from_value(nested, source_kind=f"{source_kind}.{key}", whole_string=whole_string))
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for index, item in enumerate(value, start=1):
            nested_items = _requirement_items_from_value(item, source_kind=source_kind, whole_string=True)
            for nested in nested_items:
                if not nested.get("source_ref") or nested.get("source_ref") == source_kind:
                    nested["source_ref"] = f"{source_kind}[{index}]"
                result.append(nested)
        return result
    text = str(value or "").strip()
    if not text:
        return []
    parsed = _numbered_requirement_lines(text, source_kind=source_kind)
    if parsed:
        return parsed
    if whole_string:
        return [{"text": text, "source_kind": source_kind, "source_ref": source_kind}]
    return []


def _numbered_requirement_lines(text: str, *, source_kind: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for line_index, line in enumerate(str(text or "").splitlines(), start=1):
        match = _NUMBERED_OR_BULLET_REQUIREMENT_RE.match(line)
        if not match:
            continue
        item_text = str(match.group("text") or "").strip()
        if item_text:
            result.append({"text": item_text, "source_kind": source_kind, "source_ref": f"{source_kind}:{line_index}"})
    return result


def loose_token(value: Any) -> str:
    words: list[str] = []
    for word in str(value or "").strip().lower().split():
        stripped = word.strip(" \t\r\n.,;:!?()[]{}'\"`")
        if stripped:
            words.append(stripped)
    return " ".join(words)


def _acceptance_id_at(checklist: list[dict[str, Any]], index: int) -> str:
    if index <= 0 or index > len(checklist):
        return ""
    return str(checklist[index - 1].get("id") or "").strip()


def _flatten_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return [text] if text else []
    if isinstance(value, dict):
        refs: list[str] = []
        for nested in value.values():
            refs.extend(_flatten_refs(nested))
        return refs
    if isinstance(value, (list, tuple, set)):
        refs: list[str] = []
        for nested in value:
            refs.extend(_flatten_refs(nested))
        return refs
    text = str(value or "").strip()
    return [text] if text else []


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (dict, list, tuple, set)):
            continue
        text = str(value or "").strip()
        if text:
            return text
    return ""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable

from pal.foundation import EventEnvelope
from pal.minion.checklist import build_acceptance_checklist, compact_checklist
from pal.minion.work_order import prompt_view_from_metadata
from pal.shared import (
    ChannelEnvelope,
    EndpointConfig,
    EventKind,
    PromptAssemblyContext,
    PromptFragment,
    PromptFragmentProvider,
    ResponseHandle,
    SourceKind,
    TaskContextPack,
    replace_internal_tool_names,
)
from pal.shared.payloads import extract_text_from_payload
from pal.shared.prompt_dates import today_for_timezone
from pal.shared.prompt_rendering import render_xml_block


def prompt_scaffold_summary(scaffold: dict[str, Any]) -> dict[str, Any]:
    continuity = dict(scaffold.get("continuity") or {})
    milestone = _compact_milestone(scaffold.get("current_milestone") or {})
    acceptance = _string_list(scaffold.get("acceptance_criteria"))
    if not acceptance:
        acceptance = _string_list(milestone.get("acceptance_criteria") or milestone.get("acceptance"))
    prompt_view = dict(scaffold.get("prompt_view") or {})
    checklist = _compact_prompt_checklist(prompt_view.get("checklist_projection"))
    if not checklist:
        checklist = compact_checklist(build_acceptance_checklist(acceptance), limit=12)
    repair_context = _compact_repair_context(scaffold.get("repair_context"))
    summary = {
        "task_goal": _compact_text(scaffold.get("instruction"), limit=700),
        "acceptance_checklist": checklist,
        "allowed_capability_count": len(list(scaffold.get("allowed_capabilities") or [])),
        "continuity": _compact_continuity(continuity),
        "current_milestone": milestone,
        "workspace_policy": dict(scaffold.get("workspace_policy") or {}),
        "completion_policy": dict(scaffold.get("completion_policy") or {}),
        "execution_strategy": dict(scaffold.get("execution_strategy") or {}),
        "repair_context": repair_context,
    }
    if isinstance(scaffold.get("requirements_brief"), dict):
        brief = dict(scaffold.get("requirements_brief") or {})
        summary["requirements_brief"] = {
            "present": True,
            "keys": sorted(str(key) for key in brief.keys()),
            "acceptance_criteria_count": len(list(brief.get("acceptance_criteria") or [])),
            "summary": _compact_text(brief.get("scope") or brief.get("review_scope") or brief.get("goal") or "", limit=300),
        }
    return summary


def _compact_continuity(continuity: dict[str, Any]) -> dict[str, Any]:
    recent_ledger = [_compact_event(item) for item in list(continuity.get("recent_ledger") or [])[:6]]
    completed = [_compact_milestone(item) for item in list(continuity.get("completed_milestones") or [])[:6]]
    result: dict[str, Any] = {
        "keys": sorted(str(key) for key in continuity.keys()),
        "recent_ledger_count": len(list(continuity.get("recent_ledger") or [])),
        "completed_milestone_count": len(list(continuity.get("completed_milestones") or [])),
        "task_lesson_count": len(list(continuity.get("task_lessons") or [])),
    }
    current = _compact_milestone(continuity.get("current_milestone") or {})
    if current:
        result["current_milestone"] = current
    latest = _compact_event(continuity.get("latest_checkpoint") or {})
    if latest:
        result["latest_checkpoint"] = latest
    latest_completed = _compact_event(continuity.get("latest_completed_checkpoint") or {})
    if latest_completed:
        result["latest_completed_checkpoint"] = latest_completed
    if recent_ledger:
        result["recent_ledger"] = [item for item in recent_ledger if item]
    if completed:
        result["completed_milestones"] = [item for item in completed if item]
    lessons = _compact_lessons(continuity.get("task_lessons"))
    if lessons:
        result["task_lessons"] = lessons
    return result

def _compact_milestone(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "milestone_id",
        "work_order_id",
        "milestone_index",
        "title",
        "task",
        "summary",
        "status",
        "completed",
    ):
        item = value.get(key)
        if item in (None, "", []):
            continue
        result[key] = _compact_text(item) if key in {"task", "summary"} else item
    acceptance = _string_list(value.get("acceptance_criteria") or value.get("acceptance"))
    if acceptance:
        result["acceptance_criteria"] = [_compact_text(item, limit=260) for item in acceptance[:10]]
    return result


def _compact_event(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "ledger_id",
        "checkpoint_id",
        "event_kind",
        "status",
        "phase",
        "milestone_index",
        "milestone_title",
        "created_at",
        "work_order_id",
    ):
        item = value.get(key)
        if item not in (None, "", []):
            result[key] = item
    summary = _compact_text(value.get("summary"), limit=360)
    if summary:
        result["summary"] = summary
    payload = _compact_event_payload(value.get("payload"))
    if payload:
        result["payload"] = payload
    return result


def _compact_event_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    result: dict[str, Any] = {}
    for key in (
        "phase",
        "status",
        "summary",
        "milestone_index",
        "milestone_title",
        "round",
        "tool_name",
        "target_name",
        "tool_call_count",
        "finish_reason",
        "text_preview",
        "error",
        "reason",
        "decision",
    ):
        item = payload.get(key)
        if item in (None, "", []):
            continue
        result[key] = _compact_text(item) if isinstance(item, str) else item
    return result


def _compact_prompt_checklist(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return compact_checklist([dict(item) for item in value if isinstance(item, dict)], limit=12)


def _compact_repair_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    checkpoint_repair = value.get("checkpoint_repair") if isinstance(value.get("checkpoint_repair"), dict) else {}
    current_attempt = value.get("current_repair_attempt") if isinstance(value.get("current_repair_attempt"), dict) else {}
    result: dict[str, Any] = {}
    repair_bill = value.get("repair_bill") if isinstance(value.get("repair_bill"), dict) else {}
    if repair_bill:
        result["repair_bill"] = {
            key: item
            for key, item in {
                "kind": repair_bill.get("kind"),
                "module_id": repair_bill.get("module_id"),
                "overlay_version": repair_bill.get("overlay_version"),
                "defect_kind": repair_bill.get("defect_kind"),
                "summary": _compact_text(repair_bill.get("summary"), limit=360),
                "source_bill_ids": _string_list(repair_bill.get("source_bill_ids"))[:6],
                "additional_acceptance_criteria": [
                    _compact_text(item, limit=260)
                    for item in _string_list(repair_bill.get("additional_acceptance_criteria"))[:8]
                ],
                "acceptance_criteria": [
                    {
                        key: item
                        for key, item in {
                            "id": criterion.get("id"),
                            "criterion": _compact_text(criterion.get("criterion"), limit=260),
                            "evidence_expectation": _compact_text(criterion.get("evidence_expectation"), limit=220),
                            "negative_cases": [_compact_text(case, limit=180) for case in _string_list(criterion.get("negative_cases"))[:4]],
                        }.items()
                        if item not in (None, "", [], {})
                    }
                    for criterion in list(repair_bill.get("acceptance_criteria") or [])[:8]
                    if isinstance(criterion, dict)
                ],
                "negative_cases": [
                    _compact_dict(item, limit=220)
                    for item in list(repair_bill.get("negative_cases") or [])[:6]
                    if isinstance(item, dict)
                ],
                "evidence": [
                    _compact_dict(item, limit=220)
                    for item in list(repair_bill.get("evidence") or [])[:6]
                    if isinstance(item, dict)
                ],
            }.items()
            if item not in (None, "", [], {})
        }
    active_gate_todo = value.get("active_gate_todo") if isinstance(value.get("active_gate_todo"), dict) else {}
    active_items = _compact_prompt_checklist(active_gate_todo.get("items"))
    if active_items:
        result["active_gate_todo"] = {
            key: item
            for key, item in {
                "status": active_gate_todo.get("status"),
                "summary": _compact_text(active_gate_todo.get("summary"), limit=360),
                "gate_ref": dict(active_gate_todo.get("gate_ref") or {}),
                "items": active_items,
            }.items()
            if item not in (None, "", [], {})
        }
    checklist = _compact_prompt_checklist(checkpoint_repair.get("repair_checklist"))
    if checklist:
        result["repair_checklist"] = checklist
    acceptance = _compact_prompt_checklist(checkpoint_repair.get("acceptance_checklist"))
    if acceptance:
        result["acceptance_checklist"] = acceptance
    for key in ("turn_kind", "status", "summary", "failed_checkpoint_id", "failed_commit_sha"):
        item = checkpoint_repair.get(key) or current_attempt.get(key)
        if item not in (None, "", []):
            result[key] = _compact_text(item) if isinstance(item, str) else item
    return result


def _compact_dict(value: dict[str, Any], *, limit: int = 260) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in dict(value or {}).items():
        if item in (None, "", [], {}):
            continue
        if isinstance(item, str):
            result[str(key)] = _compact_text(item, limit=limit)
        elif isinstance(item, (int, float, bool)):
            result[str(key)] = item
        elif isinstance(item, list):
            result[str(key)] = [_compact_text(child, limit=limit) for child in _string_list(item)[:6]]
        else:
            result[str(key)] = _compact_text(item, limit=limit)
    return result


def _compact_lessons(value: Any) -> list[str]:
    lessons = []
    raw_items = list(value or [])[:6] if isinstance(value, (list, tuple)) else []
    for item in raw_items:
        if isinstance(item, dict):
            text = item.get("lesson_text") or item.get("summary") or item.get("text")
        else:
            text = item
        compacted = _compact_text(text, limit=260)
        if compacted:
            lessons.append(compacted)
    return lessons


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, dict):
        return [str(item).strip() for item in value.values() if str(item or "").strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def _dedupe_ordered(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def _compact_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


@dataclass(frozen=True)
class PromptViewRenderContext:
    pack: TaskContextPack
    prompt_view: dict[str, Any]

    @property
    def role(self) -> str:
        return str(self.prompt_view.get("role") or "").strip().lower()

    @property
    def family(self) -> str:
        for source in (self.pack.metadata, self.pack.resolved_profile, self.prompt_view):
            if not isinstance(source, dict):
                continue
            family = str(source.get("family") or source.get("profile_family") or source.get("domain") or "").strip().lower()
            if family:
                return family
        profile = str(self.pack.minion_profile or "").strip().lower()
        if profile.startswith("software_engineering") or self.role in {"coder", "reviewer", "architect"}:
            return "software_engineering"
        return "generic"


class PromptViewRenderer:
    family: str = "generic"
    roles: tuple[str, ...] = ()

    def supports(self, context: PromptViewRenderContext) -> bool:
        if self.family != "*" and self.family != context.family:
            return False
        return not self.roles or context.role in self.roles

    def render(self, context: PromptViewRenderContext) -> str:
        raise NotImplementedError

    def _goal_lines(self, context: PromptViewRenderContext) -> list[str]:
        goal = _compact_text(context.pack.goal or context.pack.instruction, limit=900)
        if not goal:
            return []
        return ["## Parent Work Order Goal", goal]

    def _completion_lines(self) -> list[str]:
        return [
            "## Completion Reminder",
            "- Work only on the current scoped assignment.",
            "- Use the available checklist tools for acceptance, implementation, and repair items when they are available.",
            "- Stop after this milestone and let Pal advance the workflow.",
        ]


class GenericPromptViewRenderer(PromptViewRenderer):
    family = "*"

    def render(self, context: PromptViewRenderContext) -> str:
        view = context.prompt_view
        lines = [*self._goal_lines(context)]
        planning = _dict(view.get("planning_requirements"))
        if planning:
            lines.extend(["## Planning Requirements", _render_markdown_mapping(planning)])
        milestone = _dict(view.get("milestone"))
        task = _first_markdown_text(milestone, "task", "summary", "title")
        if task:
            lines.extend(["## Current Step", task])
        if context.role == "planner":
            lines.extend(
                [
                    "## Question Policy",
                    (
                        "Prefer a dispatchable plan with explicit conservative assumptions over ask_user_question when missing "
                        "details are normal implementation choices. Ask only for genuinely user-owned blockers."
                    ),
                ]
            )
        lines.extend(self._completion_lines())
        return _join_markdown_sections(lines)


class SoftwarePromptViewRenderer(PromptViewRenderer):
    family = "software_engineering"
    roles = ("coder", "architect", "reviewer", "")

    def render(self, context: PromptViewRenderContext) -> str:
        view = context.prompt_view
        module = _dict(view.get("module"))
        milestone = _dict(view.get("milestone"))
        metadata = _dict(milestone.get("metadata"))
        compact_module_scope = _int(milestone.get("milestone_index"), default=0) > 0
        lines: list[str] = []
        lines.extend(self._goal_lines(context))
        lines.extend(self._review_target_lines(view))
        lines.extend(self._current_milestone_lines(milestone))
        lines.extend(self._module_contract_lines(module, compact=compact_module_scope))
        lines.extend(self._interface_lines(module, view, compact=compact_module_scope))
        lines.extend(self._acceptance_lines(milestone, metadata))
        lines.extend(self._test_plan_lines(milestone, module))
        lines.extend(self._repair_lines(view))
        lines.extend(self._completion_lines())
        return _join_markdown_sections(lines)

    def _current_milestone_lines(self, milestone: dict[str, Any]) -> list[str]:
        title = _first_markdown_text(milestone, "title")
        task = _first_markdown_text(milestone, "task", "summary")
        lines = ["## Current Task"]
        if title:
            lines.append(title)
        if task and task != title:
            lines.append(task)
        if len(lines) == 1:
            lines.append("Complete the current assigned task only.")
        return lines

    def _module_contract_lines(self, module: dict[str, Any], *, compact: bool = False) -> list[str]:
        lines: list[str] = ["## Module Scope Reminder" if compact else "## Module Contract"]
        module_id = _first_markdown_text(module, "module_id", "name")
        if module_id:
            lines.append(f"Module: `{module_id}`")
        responsibility = _first_markdown_text(module, "responsibility", "purpose", "summary")
        if responsibility:
            lines.append(responsibility)
        owned_area = _string_list(module.get("owned_area") or module.get("owned_paths"))
        if owned_area:
            if compact:
                lines.append("Owned area: " + ", ".join(f"`{item}`" for item in owned_area[:12]))
            else:
                lines.extend(["### Owned Area", *_bullet_lines(owned_area)])
        if compact:
            if len(lines) == 1:
                return []
            lines.append("- The established module ownership, lifecycle, invariants, interfaces, and dependency contracts still apply to this task.")
            return lines
        ownership = _string_list(module.get("ownership"))
        if ownership:
            lines.extend(["### Ownership", *_bullet_lines(ownership)])
        lifecycle = _string_list(module.get("lifecycle"))
        if lifecycle:
            lines.extend(["### Lifecycle", *_bullet_lines(lifecycle)])
        invariants = _string_list(module.get("invariants"))
        if invariants:
            lines.extend(["### Invariants", *_bullet_lines(invariants)])
        if len(lines) == 1:
            return []
        return lines

    def _review_target_lines(self, view: dict[str, Any]) -> list[str]:
        target = _dict(view.get("review_target"))
        acceptance = _string_list(view.get("acceptance_criteria"))
        if not target and not acceptance:
            return []
        lines = ["## Review Target"]
        if target:
            for key in ("gate_kind", "kind", "module_id", "checkpoint_id", "commit_sha", "summary"):
                text = _first_markdown_text(target, key)
                if text:
                    label = key.replace("_", " ").title()
                    lines.append(f"- {label}: {text}")
            changed = _string_list(target.get("changed_files"))
            if changed:
                lines.append("- Changed files: " + ", ".join(f"`{item}`" for item in changed[:12]))
            checkpoint_git = _dict(target.get("checkpoint_git"))
            if checkpoint_git:
                commit_sha = _first_markdown_text(checkpoint_git, "commit_sha")
                stat = _first_markdown_text(checkpoint_git, "stat")
                if commit_sha:
                    lines.append(f"- Checkpoint commit: {commit_sha}")
                if stat:
                    lines.append(f"- Checkpoint stat: {stat}")
        if acceptance:
            lines.extend(["### Review Acceptance", *_bullet_lines(acceptance)])
        return lines

    def _interface_lines(self, module: dict[str, Any], view: dict[str, Any], *, compact: bool = False) -> list[str]:
        lines: list[str] = []
        provided = _unique_contract_items(_dict_list(module.get("provided_interfaces")))
        consumed = _unique_contract_items(_dict_list(module.get("consumed_interfaces")))
        relevant = _unique_contract_items(_dict_list(view.get("relevant_contracts")))
        dependency_context = _dict_list(module.get("dependency_context"))
        if compact:
            names = _contract_names([*provided, *consumed, *relevant])
            deps = [_first_markdown_text(dep, "module_id", "name") for dep in dependency_context[:8]]
            deps = [item for item in deps if item]
            if not names and not deps:
                return []
            lines.append("## Contract Reminder")
            if names:
                lines.append("- Relevant contracts: " + ", ".join(f"`{item}`" for item in names[:12]))
            if deps:
                lines.append("- Dependency modules: " + ", ".join(f"`{item}`" for item in deps[:8]))
            return lines
        if provided or consumed or relevant:
            lines.append("## Interface And Behavior")
            for title, items in (
                ("Provided Interfaces", provided),
                ("Consumed Interfaces", consumed),
                ("Relevant Contracts", relevant),
            ):
                if items:
                    lines.extend([f"### {title}", *_interface_bullets(items)])
        if dependency_context:
            lines.append("## Dependency Contracts")
            for dep in dependency_context[:8]:
                dep_lines = _dependency_contract_lines(dep)
                if dep_lines:
                    lines.extend(dep_lines)
        return lines

    def _acceptance_lines(self, milestone: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
        rich = [dict(item) for item in list(metadata.get("acceptance_checklist") or []) if isinstance(item, dict)]
        items = []
        if rich:
            for item in rich[:12]:
                text = _first_markdown_text(item, "criterion", "source_text", "text", "summary")
                if text:
                    items.append(text)
        else:
            items = _string_list(milestone.get("acceptance_criteria") or milestone.get("acceptance"))[:12]
        if not items:
            return []
        return ["## Acceptance Summary", *_bullet_lines(items)]

    def _test_plan_lines(self, milestone: dict[str, Any], module: dict[str, Any]) -> list[str]:
        plan = _dict(milestone.get("test_plan")) or _dict(module.get("test_plan"))
        if not plan:
            return []
        return ["## Test Plan", _render_markdown_mapping(plan)]

    def _repair_lines(self, view: dict[str, Any]) -> list[str]:
        repair_context = _dict(view.get("repair_context"))
        if not repair_context:
            return []
        repair_bill = _dict(repair_context.get("repair_bill"))
        repair = repair_bill or repair_context
        summary = _first_markdown_text(repair, "summary", "reason", "turn_kind")
        lines = ["## Module Repair Context"]
        module_id = _first_markdown_text(repair, "module_id")
        defect_kind = _first_markdown_text(repair, "defect_kind")
        if module_id:
            lines.append(f"- Module: `{module_id}`")
        if defect_kind:
            lines.append(f"- Defect kind: {defect_kind}")
        if summary:
            lines.append(summary)
        obligations = [
            *[
                _first_markdown_text(item, "criterion", "source_text", "text", "summary")
                for item in _dict_list(repair.get("acceptance_criteria"))
            ],
            *_string_list(repair.get("additional_acceptance_criteria")),
        ]
        obligations = [item for item in _dedupe_ordered(obligations) if item]
        if obligations:
            lines.extend(["### Required Repair Items", *_bullet_lines(obligations, limit=10)])
        negative_cases: list[str] = []
        for item in list(repair.get("negative_cases") or [])[:6]:
            if isinstance(item, dict):
                negative_cases.append(_first_markdown_text(item, "case", "input", "summary", "text"))
            else:
                negative_cases.append(str(item or "").strip())
        for item in _dict_list(repair.get("acceptance_criteria")):
            negative_cases.extend(_string_list(item.get("negative_cases")))
        negative_cases = [item for item in _dedupe_ordered(negative_cases) if item]
        if negative_cases:
            lines.extend(["### Regression Cases", *_bullet_lines(negative_cases, limit=8)])
        evidence = [
            _first_markdown_text(item, "summary", "command", "path", "text")
            for item in _dict_list(repair.get("evidence"))[:6]
        ]
        evidence = [item for item in evidence if item]
        if evidence:
            lines.extend(["### Downstream Evidence", *_bullet_lines(evidence, limit=6)])
        lines.append("These are module-level replay obligations. Do not rewrite the current milestone acceptance criteria from them.")
        return lines

    def _completion_lines(self) -> list[str]:
        return [
            "## Completion Reminder",
            "- Use `minion_checklist_read` before implementation and again before checkpointing when it is available.",
            "- Mark every required checklist item done with concrete evidence through `minion_checklist_mark_done`.",
            "- If a required item cannot be satisfied inside this milestone boundary, use `minion_checklist_mark_blocked` with the concrete blocker.",
            "- After implementation and focused verification are complete, use `checkpoint_commit` for the structured checkpoint when that tool is available.",
            "- Do not copy dependency module source files into this module; import/include declared public contracts instead.",
        ]


PROMPT_VIEW_RENDERERS: tuple[PromptViewRenderer, ...] = (
    SoftwarePromptViewRenderer(),
    GenericPromptViewRenderer(),
)


def render_prompt_view_for_llm(pack: TaskContextPack, prompt_view: dict[str, Any]) -> str:
    context = PromptViewRenderContext(pack=pack, prompt_view=dict(prompt_view or {}))
    for renderer in PROMPT_VIEW_RENDERERS:
        if renderer.supports(context):
            return renderer.render(context)
    return GenericPromptViewRenderer().render(context)


def _join_markdown_sections(lines: list[str]) -> str:
    result: list[str] = []
    previous_blank = False
    for raw in lines:
        text = str(raw or "").strip()
        if not text:
            if not previous_blank:
                result.append("")
            previous_blank = True
            continue
        if text.startswith("#") and result and result[-1] != "":
            result.append("")
        result.append(text)
        previous_blank = False
    return "\n".join(result).strip()


def _bullet_lines(items: list[Any], *, limit: int = 12) -> list[str]:
    return [f"- {_compact_text(item, limit=320)}" for item in items[:limit] if _compact_text(item, limit=320)]


def _first_markdown_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (dict, list, tuple, set)):
            continue
        text = _compact_text(value, limit=900)
        if text:
            return text
    return ""


def _dict(value: Any) -> dict[str, Any]:
    return dict(value or {}) if isinstance(value, dict) else {}


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in list(value or []) if isinstance(item, dict)]


def _int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _render_markdown_mapping(value: dict[str, Any], *, limit: int = 12) -> str:
    lines: list[str] = []
    for key, item in list(dict(value or {}).items())[:limit]:
        if item in (None, "", [], {}):
            continue
        label = str(key).replace("_", " ").strip().title()
        if isinstance(item, dict):
            nested = ", ".join(f"{nested_key}={_compact_text(nested_val, limit=160)}" for nested_key, nested_val in item.items() if nested_val not in (None, "", [], {}))
            if nested:
                lines.append(f"- {label}: {nested}")
        elif isinstance(item, list):
            text = "; ".join(_compact_text(child, limit=180) for child in _string_list(item)[:6])
            if text:
                lines.append(f"- {label}: {text}")
        else:
            lines.append(f"- {label}: {_compact_text(item, limit=320)}")
    return "\n".join(lines).strip()


def _interface_bullets(items: list[dict[str, Any]], *, limit: int = 10) -> list[str]:
    lines: list[str] = []
    for item in _unique_contract_items(items)[:limit]:
        name = _first_markdown_text(item, "name", "handle", "import_path", "source_path") or "interface"
        parts = []
        for key in ("shape", "lifecycle", "ownership", "error_behavior", "compatibility", "source_path", "import_path"):
            text = _first_markdown_text(item, key)
            if text:
                parts.append(f"{key.replace('_', ' ')}: {text}")
        suffix = f" ({'; '.join(parts)})" if parts else ""
        lines.append(f"- {name}{suffix}")
    return lines


def _unique_contract_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        key = "|".join(
            [
                _first_markdown_text(item, "name", "handle"),
                _first_markdown_text(item, "source_path"),
                _first_markdown_text(item, "import_path"),
            ]
        )
        if not key.strip("|"):
            key = _first_markdown_text(item, "shape", "summary", "contract")
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(item))
    return result


def _contract_names(items: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for item in _unique_contract_items(items):
        name = _first_markdown_text(item, "name", "handle", "import_path", "source_path")
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def _dependency_contract_lines(dep: dict[str, Any]) -> list[str]:
    module_id = _first_markdown_text(dep, "module_id", "name") or "dependency"
    lines = [f"### {module_id}"]
    responsibility = _first_markdown_text(dep, "responsibility", "purpose", "summary")
    if responsibility:
        lines.append(responsibility)
    owned = _string_list(dep.get("owned_area"))
    if owned:
        lines.append("Owned area: " + ", ".join(f"`{item}`" for item in owned[:8]))
    interfaces = _dict_list(dep.get("provided_interfaces"))
    if interfaces:
        lines.extend(_interface_bullets(interfaces, limit=6))
    return lines


@dataclass
class MinionPromptFragmentProvider(PromptFragmentProvider):
    scaffold_factory: Callable[[], dict[str, Any]]
    memory_text_factory: Callable[[], str]
    provider_id: str = "minion.runner.prompt"
    module_id: str = "minion"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        scaffold = dict(self.scaffold_factory() or {})
        fragments: list[PromptFragment] = []

        def add(section: str, title: str, content: str, priority: int, metadata: dict[str, Any] | None = None) -> None:
            rendered = replace_internal_tool_names(str(content or "").strip())
            if not rendered:
                return
            fragments.append(
                PromptFragment(
                    section=section,
                    title=title,
                    content=rendered,
                    priority=priority,
                    metadata=dict(metadata or {}),
                )
            )

        add("identity", "Minion Identity", _identity_with_current_date(scaffold), 10)
        add("behavior_guidance", "Minion Behavior", str(scaffold.get("behavior") or ""), 20)
        requirements_brief = dict(scaffold.get("requirements_brief") or {}) if isinstance(scaffold.get("requirements_brief"), dict) else {}
        if requirements_brief:
            role = _scaffold_role(scaffold)
            add("task_acceptance", _task_acceptance_title(role), _render_task_acceptance_scope(requirements_brief, role=role), 35)
            add("task_acceptance_policy", "Task Acceptance Policy", _render_task_acceptance_policy(role), 36)
        add("operating_rules", "Minion Operating Rules", _render_minion_operating_rules(scaffold), 60)

        memory_text = str(self.memory_text_factory() or "").strip()
        if memory_text:
            add("memory", "Minion Run Memory", memory_text, 90, {"block_id": "minion_run_memory"})
        retry_note = str((context.metadata or {}).get("retry_note") or "").strip()
        if retry_note:
            add("memory", "Minion Retry Guidance", retry_note, 95, {"block_id": "minion_retry_guidance"})
        skill_manual_context = _render_skill_manual_context(scaffold.get("skill_manual_context"))
        if skill_manual_context:
            add("memory", "Minion Skill Manual Context", skill_manual_context, 96, {"block_id": "skill_manual_context"})

        runtime_reminder = _render_minion_runtime_reminder(scaffold)
        add(
            "tool_efficiency",
            "Minion Runtime Reminder",
            runtime_reminder,
            100,
            {"prompt_target": "runtime_reminder", "source_priority": 80, "block_id": "tool_efficiency"},
        )
        return fragments


def _render_task_acceptance_scope(requirements_brief: dict[str, Any], *, role: str = "") -> str:
    brief = dict(requirements_brief or {})
    checklist = [dict(item) for item in list(brief.get("requirements_checklist") or []) if isinstance(item, dict)]
    lines: list[str] = []
    expose_source_items = role in {"architect", "planner"}
    if checklist and expose_source_items:
        lines.append("## Source Requirements")
        for item in checklist[:40]:
            item_id = _first_markdown_text(item, "id") or "REQ"
            source_text = _first_markdown_text(item, "source_text", "requirement", "claim", "text", "summary")
            gate_ref = _first_markdown_text(_dict(item.get("metadata")), "gate_check_ref")
            suffix = f" ({gate_ref})" if gate_ref else ""
            if source_text:
                lines.append(f"- {item_id}{suffix}: {source_text}")
        if len(checklist) > 40:
            lines.append(f"- ... {len(checklist) - 40} more source items")
    hidden = {"requirements_checklist", "requirement_checklist", "requirement_ledger", "requirements", "hard_requirements", "requirements_policy"}
    if expose_source_items:
        hidden.update({"summary", "raw", "text", "description"})
    remaining = {key: value for key, value in brief.items() if key not in hidden}
    rendered = _render_markdown_mapping(remaining, limit=20)
    if rendered:
        if lines:
            lines.append("")
        lines.append(rendered)
    return "\n".join(lines).strip()


def _render_requirements_brief(requirements_brief: dict[str, Any]) -> str:
    return _render_task_acceptance_scope(requirements_brief, role="architect")


def _render_task_acceptance_policy(role: str = "") -> str:
    if role in {"architect", "planner"}:
        return (
            "Treat the source items above as the input ledger for planning. The plan must preserve each source item without "
            "weakening, map it into concrete constraints/modules/milestones/acceptance criteria, and prove that mapping with "
            "real gate_check_refs such as gate:0 when a source gate contract is present. Boundary decisions, negative cases, "
            "interfaces, and verification criteria may make the source items implementable, but they must not add new product "
            "scope. Ask only for user-owned ambiguity."
        )
    return (
        "Treat the visible task acceptance scope and current work view as the execution contract. Do not invent new acceptance "
        "criteria or product scope. If the current module or milestone acceptance conflicts with source, repo, or tool evidence, "
        "report the conflict or blocker instead of silently replacing the assigned contract."
    )


def _render_requirements_policy() -> str:
    return _render_task_acceptance_policy("architect")


def _scaffold_role(scaffold: dict[str, Any]) -> str:
    prompt_view = _dict(scaffold.get("prompt_view"))
    role = str(prompt_view.get("role") or scaffold.get("role") or "").strip().lower()
    if role:
        return role
    profile = str(scaffold.get("minion_profile") or "").strip().lower()
    if profile.endswith(".architect") or profile == "architect":
        return "architect"
    return ""


def _task_acceptance_title(role: str) -> str:
    return "Source Requirements For Planning" if role in {"architect", "planner"} else "Task Acceptance Scope"


def _render_skill_manual_context(value: Any) -> str:
    items = [dict(item) for item in list(value or []) if isinstance(item, dict)]
    if not items:
        return ""
    lines = [
        "<skill_manual_context>",
        "Reference material for activated minion skills. Use it only when relevant to the current scoped milestone.",
    ]
    for item in items:
        skill_id = _compact_text(item.get("skill_id"), limit=120)
        title = _compact_text(item.get("title") or skill_id, limit=160)
        summary = _compact_text(item.get("summary"), limit=320)
        manual = str(item.get("manual_text") or "").strip()
        if not skill_id or not manual:
            continue
        lines.extend(["", f"## {title or skill_id}", f"- skill_id: {skill_id}"])
        if summary:
            lines.append(f"- summary: {summary}")
        lines.extend(["", manual])
    lines.append("</skill_manual_context>")
    return "\n".join(lines).strip()


def _render_minion_operating_rules(scaffold: dict[str, Any]) -> str:
    completion_policy = scaffold.get("completion_policy") or {}
    testing_guidance = ""
    if isinstance(completion_policy, dict) and bool(completion_policy.get("requires_developer_tests")):
        testing_guidance = (
            "Completion requires developer test evidence: before completing, state the focused test plan, "
            "run the relevant tests/checks available through this run's tools, fix failures you caused, "
            "and report blocked instead of completed if tests cannot be run or cannot pass with concrete evidence.\n"
        )
    artifact_completion_guidance = ""
    if isinstance(completion_policy, dict) and bool(completion_policy.get("allow_artifact_evidence")):
        artifact_completion_guidance = (
            "This milestone may complete with artifact evidence when it is verification-only and there are no source, "
            "test, doc, or config changes to commit. In that case, write a verification report artifact with the "
            "commands/checks run and finish with a concise summary; do not call `op_minion_checkpoint_commit` solely "
            "to create an empty checkpoint commit.\n"
        )
    return (
        "Your context is the current scoped milestone and the tools made available by Pal.\n"
        "Treat the prompt work view as the complete scoped assignment; do not infer or implement hidden modules or later milestones.\n"
        "Use only the tools exposed to this run. Report by milestone, never by percentage or ETA.\n"
        "If capability evidence is required, use a relevant listed capability before completing the milestone.\n"
        f"{testing_guidance}"
        f"{artifact_completion_guidance}"
        "If completion evidence cannot be produced, report blocked instead of completed.\n"
        "When completion policy requires git_commit, do not run git add, git commit, or other checkpoint git mutation commands through shell or the git wrapper. "
        "After implementing and verifying the milestone, call `op_minion_checkpoint_commit` to create the structured checkpoint commit in the minion workspace branch.\n"
        "Do not create or rely on committing generated build/cache artifacts such as __pycache__, .pytest_cache, .o, .obj, .a, .so, .dylib, .dll, .exe, class files, coverage output, build directories, .minion/artifacts reports, or legacy minion_outputs reports.\n"
        "When artifact tools are available, write planner/reviewer deliverables and any long structured output to workspace.artifact_dir with artifact tools; keep the final chat summary short and point to the artifact.\n"
        "Use `op_minion_artifact_write` for one complete coherent file. Use `op_minion_artifact_edit` append for long deliverables split into coherent sections, or replace only when rewriting the complete artifact. Do not rely on final chat text for long plans or reports.\n"
        "When `op_minion_memory_candidate_write` is available and the run teaches something genuinely reusable, write a concise memory candidate there instead of asking Pal to remember it directly.\n"
        "If a tool/capability call fails because of an obvious schema, argument, path, or local input mistake, correct the call directly.\n"
        "When the current milestone is complete, stop with a concise milestone summary. Pal will ask the user before absorbing minion memory candidates."
    )


def render_minion_system_prompt(scaffold: dict[str, Any]) -> str:
    requirements_brief = dict(scaffold.get("requirements_brief") or {}) if isinstance(scaffold.get("requirements_brief"), dict) else {}
    role = _scaffold_role(scaffold)
    blocks = [
        ("identity", replace_internal_tool_names(_identity_with_current_date(scaffold))),
        ("behavior_guidance", replace_internal_tool_names(str(scaffold.get("behavior") or "").strip())),
        ("task_acceptance", replace_internal_tool_names(_render_task_acceptance_scope(requirements_brief, role=role) if requirements_brief else "")),
        ("task_acceptance_policy", replace_internal_tool_names(_render_task_acceptance_policy(role) if requirements_brief else "")),
        ("operating_rules", replace_internal_tool_names(_render_minion_operating_rules(scaffold)).strip()),
    ]
    return "\n\n".join(render_xml_block(tag, content) for tag, content in blocks if str(content or "").strip()).strip()


def _identity_with_current_date(scaffold: dict[str, Any]) -> str:
    identity = str(scaffold.get("identity") or "").strip()
    date_value = str(scaffold.get("current_date") or "").strip()
    if not date_value:
        date_value = today_for_timezone(str(scaffold.get("timezone") or ""))
    date_line = f"Today's date is {date_value}."
    if date_line in identity:
        return identity
    return "\n".join(part for part in (identity, date_line) if part).strip()


def build_minion_task_envelope(pack: TaskContextPack, *, minion_id: str, run_id: str) -> ChannelEnvelope:
    endpoint_id = f"minion:{run_id}"
    return ChannelEnvelope(
        event=EventEnvelope(
            event_kind=EventKind.USER_MESSAGE,
            source_kind=SourceKind.MINION,
            payload={
                "text": render_minion_task_prompt(pack),
                "work_order_id": pack.work_order_id,
                "minion_profile": pack.minion_profile,
            },
            correlation_id=run_id,
        ),
        endpoint=EndpointConfig(
            endpoint_id=endpoint_id,
            channel_kind="stdio",
            binding_key=run_id,
            send_policy={"route": "minion_manager"},
        ),
        response_handle=ResponseHandle(
            endpoint_id=endpoint_id,
            reply_target={
                "run_id": run_id,
                "minion_id": minion_id,
                "work_order_id": pack.work_order_id,
            },
        ),
    )


def minion_primary_input(envelope: ChannelEnvelope) -> str:
    text = extract_text_from_payload(envelope.event.payload).strip()
    if text:
        return text
    return "No sanitized minion task text was provided. Report blocked."


def render_minion_task_prompt(pack: TaskContextPack) -> str:
    prompt_view = prompt_view_from_pack(pack)
    if prompt_view:
        return render_prompt_view_for_llm(pack, prompt_view)
    lines: list[str] = ["# Minion Task"]
    goal = _compact_text(pack.goal, limit=900)
    if goal:
        lines.extend(["## Goal", goal])
    instruction = _compact_text(pack.instruction, limit=900)
    if instruction:
        lines.extend(["## Instruction", instruction])
    current_milestone = _current_milestone_from_pack(pack)
    if current_milestone:
        title = _first_markdown_text(current_milestone, "title")
        task = _first_markdown_text(current_milestone, "task", "summary")
        current_lines = ["## Current Task"]
        if title:
            current_lines.append(title)
        if task and task != title:
            current_lines.append(task)
        if len(current_lines) > 1:
            lines.extend(current_lines)
    current_acceptance = _string_list(current_milestone.get("acceptance_criteria") or current_milestone.get("acceptance"))
    acceptance = [item for item in (current_acceptance or _string_list(pack.acceptance_criteria)) if item]
    if isinstance(pack.metadata.get("requirements_brief"), dict):
        requirements_brief = dict(pack.metadata.get("requirements_brief") or {})
        brief_acceptance = [str(item).strip() for item in list(requirements_brief.get("acceptance_criteria") or []) if str(item or "").strip()]
        if brief_acceptance:
            if acceptance:
                lines.extend(["## Executor Acceptance Addendum", *_bullet_lines(acceptance)])
            acceptance = brief_acceptance
        rendered_brief = _render_task_acceptance_scope(requirements_brief, role="")
        if rendered_brief:
            lines.extend(["## Task Acceptance Scope", rendered_brief])
        lines.extend(["## Task Acceptance Policy", _render_task_acceptance_policy("")])
    if acceptance:
        lines.extend(["## Acceptance Criteria", *_bullet_lines(acceptance)])
    references = _render_fallback_reference_lines(pack)
    if references:
        lines.extend(["## References", *references])
    if pack.memory_pack:
        lines.extend(["## Memory Context", "A memory pack is available for this run. Use it only when it is directly relevant."])
    lines.extend(
        [
            "## Completion Reminder",
            "- Work only on this bounded task.",
            "- Use available tools for evidence before reporting completion.",
            "- Report blocked with concrete evidence if the task cannot be completed safely.",
        ]
    )
    return _join_markdown_sections(lines)


def _current_milestone_from_pack(pack: TaskContextPack) -> dict[str, Any]:
    current = _dict((pack.continuity or {}).get("current_milestone"))
    if current:
        return current
    metadata = dict(pack.metadata or {})
    module_execution = _dict(metadata.get("module_execution"))
    milestones = [dict(item) for item in list(metadata.get("milestones") or []) if isinstance(item, dict)]
    if not milestones:
        return {}
    index = _int(module_execution.get("current_milestone_index"), default=0)
    index = max(0, min(index, len(milestones) - 1))
    current = dict(milestones[index])
    current.setdefault("milestone_index", index)
    return current


def _render_fallback_reference_lines(pack: TaskContextPack) -> list[str]:
    lines: list[str] = []
    workspace = _prompt_safe_workspace(pack.workspace)
    for key in ("repo_path", "target_repo_path", "task_repo_path", "artifact_dir"):
        value = str(workspace.get(key) or "").strip()
        if value:
            label = key.replace("_", " ")
            lines.append(f"- {label}: `{value}`")
    artifacts = _prompt_safe_artifact_refs(pack.metadata.get("supporting_artifacts") or pack.artifacts)
    for item in artifacts[:8]:
        parts = []
        for key in ("kind", "role", "module_id", "path"):
            value = str(item.get(key) or "").strip()
            if value:
                parts.append(f"{key.replace('_', ' ')}: {value}")
        if parts:
            lines.append("- artifact: " + "; ".join(parts))
    return lines


def prompt_view_from_pack(pack: TaskContextPack) -> dict[str, Any]:
    metadata = dict(pack.metadata or {})
    prompt_view = prompt_view_from_metadata(metadata, workspace=dict(pack.workspace))
    if prompt_view:
        return prompt_view
    continuity = dict(pack.continuity or {})
    if isinstance(continuity.get("prompt_view"), dict):
        prompt_view = prompt_view_from_metadata({"prompt_view": dict(continuity.get("prompt_view") or {})}, workspace=dict(pack.workspace))
        return prompt_view
    return {}


def _prompt_safe_workspace(workspace: dict[str, Any]) -> dict[str, str]:
    allowed = {"repo_path", "artifact_dir", "task_repo_path", "target_repo_path"}
    return {key: str(value) for key, value in dict(workspace or {}).items() if key in allowed and str(value or "").strip()}


def _render_minion_runtime_reminder(scaffold: dict[str, Any]) -> str:
    allowed = {str(item).strip() for item in list((scaffold or {}).get("allowed_capabilities") or []) if str(item).strip()}
    lines = [
        "Minion runtime reminder:",
        "- Treat this reminder as behavior-routing guidance for choosing the right capability. The system prompt and capability policy still define the principles and priority order.",
        "- Work only on the current scoped milestone and available tools; do not start hidden or later work.",
        "- Choose the smallest available capability that matches the immediate intent, then reassess after seeing its result.",
        "- Do not guess unavailable capability names; if the right capability is absent, use the closest safe path or report the limitation.",
    ]
    if "op_exec_shell" in allowed:
        lines.append(
            "- Use process execution only when the task needs a real command, build, test, script, probe, or read-only repository verification."
        )
    if "op_minion_checkpoint_commit" in allowed:
        lines.append("- When a structured checkpoint is required, complete implementation and verification before submitting it.")
    if allowed & {"op_minion_review_gate_submit", "op_minion_review_checkpoint"}:
        lines.append("- Reviewer completion requires a structured gate result; prose-only approval is not enough.")
        lines.append("- If review evidence already covers the binding contract, submit the review gate now instead of gathering optional extra evidence.")
    return "\n".join(lines).strip()


def _prompt_safe_artifact_refs(raw: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in list(raw or []):
        if not isinstance(item, dict):
            continue
        safe = {
            "kind": str(item.get("kind") or item.get("type") or "").strip(),
            "role": str(item.get("role") or "").strip(),
        }
        ref = dict(item.get("ref") or {}) if isinstance(item.get("ref"), dict) else item
        path = str(ref.get("path") or ref.get("relative_path") or "").strip()
        if path:
            safe["path"] = path
        if str(item.get("module_id") or "").strip():
            safe["module_id"] = str(item.get("module_id") or "").strip()
        safe = {key: value for key, value in safe.items() if value}
        if safe:
            result.append(safe)
    return result

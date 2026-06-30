from __future__ import annotations

import json
from typing import Any

from pal.foundation import EventEnvelope
from pal.minion.checklist import build_acceptance_checklist, compact_checklist
from pal.minion.work_order import prompt_view_from_metadata
from pal.shared import (
    ChannelEnvelope,
    EndpointConfig,
    EventKind,
    ResponseHandle,
    SourceKind,
    TaskContextPack,
    llm_tool_name,
    replace_internal_tool_names,
)
from pal.shared.payloads import extract_text_from_payload
from pal.shared.prompt_rendering import render_runtime_reminder, render_system_reminder, render_xml_block


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
    return {
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


def _compact_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def render_minion_system_prompt(scaffold: dict[str, Any]) -> str:
    completion_policy = scaffold.get("completion_policy") or {}
    testing_guidance = ""
    if isinstance(completion_policy, dict) and bool(completion_policy.get("requires_developer_tests")):
        testing_guidance = (
            "Completion requires developer test evidence: before completing, state the focused test plan, "
            "run the relevant tests/checks available through listed capabilities, fix failures you caused, "
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
    operating_rules = (
        "Your context is the prompt work view, the current milestone, and the listed capabilities.\n"
        "Treat the prompt work view as the complete scoped assignment; do not infer or implement hidden modules or later milestones.\n"
        "Use only the listed capabilities. Report by milestone, never by percentage or ETA.\n"
        "Use `op_memory_recall` when prior Pal experience, project lessons, or user preferences may materially improve the result.\n"
        "If capability evidence is required, use a relevant listed capability before completing the milestone.\n"
        f"{testing_guidance}"
        f"{artifact_completion_guidance}"
        "If completion evidence cannot be produced, report blocked instead of completed.\n"
        "When completion policy requires git_commit, do not run git add, git commit, or other checkpoint git mutation commands through shell or the git wrapper. "
        "After implementing and verifying the milestone, call `op_minion_checkpoint_commit` to create the structured checkpoint commit in the minion workspace branch.\n"
        "Do not create or rely on committing generated build/cache artifacts such as __pycache__, .pytest_cache, .o, .obj, .a, .so, .dylib, .dll, .exe, class files, coverage output, build directories, or minion_outputs reports.\n"
        "When `op_minion_artifact_write` or `op_minion_artifact_edit` is available, write planner/reviewer deliverables and any long structured output to workspace.artifact_dir with artifact tools; keep the final chat summary short and point to the artifact.\n"
        "Use `op_minion_artifact_write` for one complete coherent file. Use `op_minion_artifact_edit` append for long deliverables split into coherent sections, or replace only when rewriting the complete artifact. Do not rely on final chat text for long plans or reports.\n"
        "Artifact output must satisfy the current output_contract. If the output_contract requires JSON, write a .json artifact with application/json content containing exactly that JSON object; do not turn it into Markdown prose just because it is an artifact.\n"
        "When `op_minion_memory_candidate_write` is available and the run teaches something genuinely reusable, write a concise memory candidate there instead of asking Pal to remember it directly.\n"
        "If a tool/capability call fails because of an obvious schema, argument, path, or local input mistake, correct the call directly.\n"
        "If a tool/capability call fails and the next step is unclear, repeated retries would be guesswork, or the failure may have prior Pal/project repair history, use `memory_recall` when it is listed below before retrying, debugging further, or reporting blocked.\n"
        "When the current milestone is complete, stop with a concise milestone summary. "
        "Pal will ask the user before absorbing minion memory candidates."
    )
    allowed_aliases = [llm_tool_name(item) for item in list(scaffold.get("allowed_capabilities") or [])]
    blocks = [
        ("identity", replace_internal_tool_names(str(scaffold.get("identity") or "").strip())),
        ("behavior_guidance", replace_internal_tool_names(str(scaffold.get("behavior") or "").strip())),
        ("system-reminder", replace_internal_tool_names(_render_skill_manual_context(scaffold.get("skill_manual_context")))),
        ("operating_rules", replace_internal_tool_names(operating_rules).strip()),
        ("workspace_policy", replace_internal_tool_names(json.dumps(scaffold.get("workspace_policy") or {}, ensure_ascii=False, sort_keys=True))),
        ("completion_policy", replace_internal_tool_names(json.dumps(scaffold.get("completion_policy") or {}, ensure_ascii=False, sort_keys=True))),
        ("execution_strategy", replace_internal_tool_names(json.dumps(scaffold.get("execution_strategy") or {}, ensure_ascii=False, sort_keys=True))),
        ("output_contract", replace_internal_tool_names(str(scaffold.get("output_contract") or "").strip())),
        ("allowed_capabilities", json.dumps(allowed_aliases, ensure_ascii=False)),
    ]
    return "\n\n".join(render_xml_block(tag, content) for tag, content in blocks if str(content or "").strip()).strip()


def build_minion_prompt_messages(
    *,
    scaffold: dict[str, Any],
    channel_envelope: ChannelEnvelope,
    memory_text: str,
    retry_note: str,
    tool_protocol_messages: list[dict[str, Any]],
    include_tool_protocol: bool = True,
) -> list[dict[str, Any]]:
    system = render_minion_system_prompt(scaffold)
    retry_note = str(retry_note or "").strip()
    memory_text = str(memory_text or "").strip()
    runtime_reminder = _render_minion_runtime_reminder(scaffold)
    if not include_tool_protocol and retry_note and not tool_protocol_messages:
        task_parts: list[dict[str, Any]] = []
        if memory_text:
            task_parts.append({"type": "text", "text": render_system_reminder(f"Minion run memory:\n{memory_text}")})
        task_parts.append({"type": "text", "text": minion_primary_input(channel_envelope)})
        if runtime_reminder:
            task_parts.append({"type": "text", "text": render_runtime_reminder(runtime_reminder)})
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": coerce_user_content_parts(task_parts)},
            {"role": "user", "content": render_system_reminder(f"Minion retry guidance:\n{retry_note}")},
        ]
    protocol_messages = list(tool_protocol_messages) if include_tool_protocol else []
    task_parts = []
    if memory_text and (not include_tool_protocol or not protocol_messages):
        task_parts.append({"type": "text", "text": render_system_reminder(f"Minion run memory:\n{memory_text}")})
    if retry_note and not include_tool_protocol:
        task_parts.append({"type": "text", "text": render_system_reminder(f"Minion retry guidance:\n{retry_note}")})
    task_parts.append({"type": "text", "text": minion_primary_input(channel_envelope)})
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": coerce_user_content_parts(task_parts)},
        *protocol_messages,
    ]
    trailing_parts: list[dict[str, Any]] = []
    if runtime_reminder:
        trailing_parts.append({"type": "text", "text": render_runtime_reminder(runtime_reminder)})
    if memory_text and include_tool_protocol and protocol_messages:
        trailing_parts.append({"type": "text", "text": render_runtime_reminder(f"Minion run memory:\n{memory_text}")})
    if retry_note and include_tool_protocol:
        trailing_parts.append({"type": "text", "text": render_runtime_reminder(f"Minion retry guidance:\n{retry_note}")})
    if trailing_parts:
        messages.append({"role": "user", "content": coerce_user_content_parts(trailing_parts)})
    return messages


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
    return json.dumps({"instruction": "No sanitized minion task text was provided. Report blocked."}, ensure_ascii=False, sort_keys=True)


def render_minion_task_prompt(pack: TaskContextPack) -> str:
    prompt_view = prompt_view_from_pack(pack)
    if prompt_view:
        instructions = [
            "Execute only the scoped work in this prompt_view.",
            "Use module contracts instead of inferring other module internals.",
            "Do not use undeclared cross-module imports anywhere, including join; compose only through declared public interfaces, exported facades, or prelude contracts.",
            "Do not start later milestones or hidden plan steps; stop after this milestone and wait for manager input.",
        ]
        if str(prompt_view.get("role") or "").strip().lower() == "planner":
            instructions.append(
                "Prefer a dispatchable plan with explicit conservative assumptions over ask_user_question when missing details are normal implementation choices; ask only for genuinely user-owned blockers."
            )
        payload = {
            "goal": pack.goal,
            "prompt_view": prompt_view,
            "instructions": instructions,
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload = {
        "goal": pack.goal,
        "instruction": pack.instruction,
        "acceptance_criteria": list(pack.acceptance_criteria),
        "workspace": _prompt_safe_workspace(pack.workspace),
        "supporting_artifacts": _prompt_safe_artifact_refs(pack.metadata.get("supporting_artifacts") or pack.artifacts),
    }
    if pack.memory_pack:
        payload["memory_pack"] = dict(pack.memory_pack)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def prompt_view_from_pack(pack: TaskContextPack) -> dict[str, Any]:
    metadata = dict(pack.metadata or {})
    prompt_view = prompt_view_from_metadata(metadata, workspace=dict(pack.workspace))
    if prompt_view:
        if pack.allowed_capabilities:
            prompt_view["allowed_capabilities"] = [llm_tool_name(item) for item in list(pack.allowed_capabilities)]
        return prompt_view
    continuity = dict(pack.continuity or {})
    if isinstance(continuity.get("prompt_view"), dict):
        prompt_view = prompt_view_from_metadata({"prompt_view": dict(continuity.get("prompt_view") or {})}, workspace=dict(pack.workspace))
        if prompt_view and pack.allowed_capabilities:
            prompt_view["allowed_capabilities"] = [llm_tool_name(item) for item in list(pack.allowed_capabilities)]
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
        "- Work only on the current scoped milestone and listed capabilities; do not start hidden or later work.",
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


def coerce_user_content_parts(parts: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    if len(parts) == 1 and parts[0].get("type") == "text":
        return str(parts[0].get("text") or "")
    return parts


def _render_skill_manual_context(items: Any) -> str:
    blocks: list[str] = []
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get("skill_id") or "").strip()
        manual_text = str(item.get("manual_text") or "").strip()
        if not skill_id or not manual_text:
            continue
        title = str(item.get("title") or skill_id).strip()
        summary = str(item.get("summary") or "").strip()
        use_when = str(item.get("use_when") or "").strip()
        avoid_when = str(item.get("avoid_when") or "").strip()
        parts = [
            f"Skill: {skill_id} - {title}",
            f"Summary: {summary}" if summary else "",
            f"Use when: {use_when}" if use_when else "",
            f"Avoid when: {avoid_when}" if avoid_when else "",
            "Manual:",
            manual_text,
        ]
        blocks.append("\n".join(part for part in parts if part).strip())
    return "\n\n".join(blocks).strip()

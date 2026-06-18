from __future__ import annotations

import json
from typing import Any

from pal.foundation import EventEnvelope
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
    return {
        "instruction_chars": len(str(scaffold.get("instruction") or "")),
        "acceptance_criteria_count": len(list(scaffold.get("acceptance_criteria") or [])),
        "allowed_capability_count": len(list(scaffold.get("allowed_capabilities") or [])),
        "continuity": {
            "keys": sorted(str(key) for key in continuity.keys()),
            "recent_ledger_count": len(list(continuity.get("recent_ledger") or [])),
            "completed_milestone_count": len(list(continuity.get("completed_milestones") or [])),
            "task_lesson_count": len(list(continuity.get("task_lessons") or [])),
        },
        "current_milestone": dict(scaffold.get("current_milestone") or {}),
        "workspace_policy": dict(scaffold.get("workspace_policy") or {}),
        "completion_policy": dict(scaffold.get("completion_policy") or {}),
    }


def render_minion_system_prompt(scaffold: dict[str, Any]) -> str:
    completion_policy = scaffold.get("completion_policy") or {}
    testing_guidance = ""
    if isinstance(completion_policy, dict) and bool(completion_policy.get("requires_developer_tests")):
        testing_guidance = (
            "Completion requires developer test evidence: before completing, state the focused test plan, "
            "run the relevant tests/checks available through listed capabilities, fix failures you caused, "
            "and report blocked instead of completed if tests cannot be run or cannot pass with concrete evidence.\n"
        )
    operating_rules = (
        "Your context is the prompt work view, the current milestone, and the listed capabilities.\n"
        "Treat the prompt work view as the complete scoped assignment; do not infer or implement hidden modules or later milestones.\n"
        "Use only the listed capabilities. Report by milestone, never by percentage or ETA.\n"
        "Use `op_memory_recall` when prior Pal experience, project lessons, or user preferences may materially improve the result.\n"
        "If capability evidence is required, use a relevant listed capability before completing the milestone.\n"
        f"{testing_guidance}"
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

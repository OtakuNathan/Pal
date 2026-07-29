from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable

from pal.foundation import EventEnvelope
from pal.shared import (
    ChannelEnvelope,
    EndpointConfig,
    EventKind,
    PromptAssemblyContext,
    PromptFragment,
    PromptFragmentProvider,
    ResponseHandle,
    SourceKind,
    MinionInvocationPack,
)
from pal.shared.payloads import extract_text_from_payload


def prompt_scaffold_summary(scaffold: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_goal": str(scaffold.get("instruction") or "")[:700],
        "acceptance_criteria": [str(item) for item in list(scaffold.get("acceptance_criteria") or [])[:20]],
        "allowed_capability_count": len(list(scaffold.get("allowed_capabilities") or [])),
        "workspace_policy": dict(scaffold.get("workspace_policy") or {}),
        "completion_policy": dict(scaffold.get("completion_policy") or {}),
        "workflow_model": "contract_v2",
    }


@dataclass
class MinionPromptFragmentProvider(PromptFragmentProvider):
    scaffold_factory: Callable[[], dict[str, Any]]
    role_context_factory: Callable[[], str]
    provider_id: str = "minion.v2.worker.prompt"
    module_id: str = "minion"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        scaffold = dict(self.scaffold_factory() or {})
        fragments: list[PromptFragment] = []

        def add(
            section: str,
            title: str,
            content: str,
            priority: int,
            *,
            metadata: dict[str, Any] | None = None,
        ) -> None:
            text = str(content or "").strip()
            if text:
                fragments.append(
                    PromptFragment(
                        section=section,
                        title=title,
                        content=text,
                        priority=priority,
                        metadata=dict(metadata or {}),
                    )
                )

        add("identity", "Minion Identity", str(scaffold.get("identity") or ""), 10)
        add("behavior_guidance", "Role Contract", str(scaffold.get("behavior") or ""), 20)
        add("task_acceptance", "Bound Invocation", _render_bound_invocation(scaffold), 35)
        role_context = str(self.role_context_factory() or "").strip()
        if role_context:
            add(
                "memory",
                "Role Working State",
                role_context,
                54,
                metadata={
                    "block_id": "minion_role_working_state",
                    "raw_user_context": True,
                    "runtime_context_kind": "role_state",
                },
            )
        add("operating_rules", "Execution Rules", _render_execution_rules(scaffold), 60)
        add("output_contract", "Output Contract", str(scaffold.get("output_contract") or ""), 70)
        retry = str(dict(context.metadata or {}).get("retry_note") or "")
        if retry:
            add(
                "memory",
                "Retry Guidance",
                retry,
                95,
                metadata={
                    "block_id": "minion_retry_guidance",
                    "raw_user_context": True,
                    "runtime_context_kind": "retry_guidance",
                },
            )
        return fragments


def build_minion_task_envelope(pack: MinionInvocationPack, *, minion_id: str, run_id: str) -> ChannelEnvelope:
    endpoint_id = f"minion:{run_id}"
    return ChannelEnvelope(
        event=EventEnvelope(
            event_kind=EventKind.USER_MESSAGE,
            source_kind=SourceKind.MINION,
            payload={
                "text": render_minion_task_prompt(pack),
                "invocation_id": pack.invocation_id,
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
            reply_target={"run_id": run_id, "minion_id": minion_id, "invocation_id": pack.invocation_id},
        ),
    )


def minion_primary_input(envelope: ChannelEnvelope) -> str:
    return extract_text_from_payload(envelope.event.payload).strip() or "No worker instruction was provided. Report blocked."


def render_minion_task_prompt(pack: MinionInvocationPack) -> str:
    lines = ["# Contract Invocation"]
    goal = str(pack.instruction or pack.goal or "").strip()
    if goal:
        lines.extend(["", "## Assignment", goal])
    skill_reminders = _initial_skill_reminders(pack)
    if skill_reminders:
        lines.extend(["", *skill_reminders])
    lines.extend(
        [
            "",
            "## Execution Discipline — High Priority",
            *_execution_discipline_lines(pack),
        ]
    )
    if pack.acceptance_criteria:
        lines.extend(["", "## Invocation Acceptance"])
        lines.extend(f"- {item}" for item in pack.acceptance_criteria)
    references = [dict(item) for item in list(pack.workspace.get("reference_paths") or []) if isinstance(item, dict)]
    if references:
        lines.extend(["", "## Immutable Inputs"])
        for item in references:
            name = str(item.get("name") or "")
            includes = [str(value).strip() for value in list(item.get("include") or []) if str(value).strip()]
            path = str(item.get("path") or "").strip()
            details = [
                "read-only semantic input",
                "access=ordinary file/search tools",
                f"truth_source={bool(item.get('truth_source'))}",
            ]
            if not bool(item.get("bound_input")):
                visible_path = path or f"/pal/references/{name}"
                details.append(f"path={visible_path}")
                if len(includes) == 1 and not any(character in includes[0] for character in "*?["):
                    details.append(
                        "read_file_args="
                        + json.dumps(
                            {"file_path": visible_path},
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    )
            if includes:
                details.append(
                    "projected_paths="
                    + json.dumps(includes, ensure_ascii=False, separators=(",", ":"))
                )
            lines.append(f"- reference:{name}: " + "; ".join(details))
        lines.extend(
            [
                "",
                "## Tool Efficiency",
                "- Before reading a reference, briefly investigate what the supplied path currently contains and choose the appropriate visible tool. Do not assume the path is a file or call read_file on it before establishing that it is the relevant file.",
                "- When exact read_file_args are already supplied, use that exact file path directly when the reference is needed; do not investigate it again.",
                "- Keep reference investigation bounded, read only what the current question requires, and reuse what you already learned instead of repeating discovery.",
            ]
        )
    lines.extend(
        [
            "",
            "## Boundary",
            "- This is one durable role invocation, not an autonomous workflow.",
            "- Do not create architecture, acceptance authority, hidden follow-up work, milestones, or checkpoints.",
            "- Use only visible capabilities and the bound workspace/reference roots.",
            "- Immutable inputs are lookup sources, not a mandatory reading checklist. Read one only when the current prompt, source, contract, diff, or evidence is insufficient to decide a role obligation or named question; stop once the evidence is decisive.",
        ]
    )
    return "\n".join(lines)


def _initial_skill_reminders(pack: MinionInvocationPack) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in list(dict(pack.metadata or {}).get("initial_skill_injections") or []):
        if not isinstance(item, dict):
            continue
        skill_id = str(item.get("skill_id") or "").strip()
        reminder = str(item.get("system_reminder") or "").strip()
        if not skill_id or not reminder or skill_id in seen:
            continue
        seen.add(skill_id)
        result.append(reminder)
    return result


def _execution_discipline_lines(pack: MinionInvocationPack) -> list[str]:
    lines = [
        "- Preserve contract correctness and role boundaries. Efficiency means eliminating duplicate work, never skipping decisive evidence.",
        "- Make one bounded pass over the owned scope and only the evidence needed for this role. Once the next action is clear, act; do not reopen settled questions unless new evidence contradicts them.",
        "- Request independent reads, searches, or checks together in one response. Sequence only operations whose arguments or safety depend on an earlier result.",
        "- Reuse content and passing results already visible in this logical session. If read_file reports that content is unchanged, refer to the earlier result and do not request it again.",
        "- Prefer the smallest contract-complete action. Do not add optional abstraction, evidence, or polish after this role's completion conditions are satisfied.",
    ]
    role = str(dict(dict(pack.metadata or {}).get("minion_v2") or {}).get("role") or "")
    if role == "implementation":
        lines.append(
            "- Implementation: once the owned contract, edit path, and one sufficient validation path are clear, implement directly. When the checklist is complete and focused checks pass, call candidate_submit immediately."
        )
    elif role == "verifier":
        lines.extend(
            [
                "- Verification: treat VerificationPolicy as the bounded checklist. Replay required cases, inspect current diff risk, and submit as soon as decisive evidence covers it; do not accumulate optional evidence or repeat unchanged passing checks.",
                "- Use the Manager-prepared verification LSP tool. Do not invoke language-server executables or create compile configuration through shell.",
            ]
        )
    elif role == "architect":
        lines.append(
            "- Architecture: after one requirements-consistency pass, declare the smallest complete module and contract graph. Do not rehearse implementation; call architecture_submit as soon as the declared completion conditions hold."
        )
    elif role == "reviewer":
        lines.append(
            "- Review: perform one breadth-first semantic pass, batch all material findings, and submit once. Do not reopen accepted surfaces unless current evidence contradicts them."
        )
    return lines


def prompt_view_from_pack(pack: MinionInvocationPack) -> dict[str, Any]:
    metadata = dict(pack.metadata or {})
    value = metadata.get("unit_work_view") or metadata.get("prompt_view")
    return dict(value) if isinstance(value, dict) else {}


def _render_bound_invocation(scaffold: dict[str, Any]) -> str:
    lines = [str(scaffold.get("instruction") or "").strip()]
    acceptance = [str(item) for item in list(scaffold.get("acceptance_criteria") or []) if str(item).strip()]
    if acceptance:
        lines.append("\nAcceptance:")
        lines.extend(f"- {item}" for item in acceptance)
    return "\n".join(item for item in lines if item)


def _render_execution_rules(scaffold: dict[str, Any]) -> str:
    allowed = [str(item) for item in list(scaffold.get("visible_capabilities") or [])]
    return "\n".join(
        (
            "- Work only on the bound role invocation.",
            "- Architecture roles must use their bound architecture tools and output contract; they cannot invent alternate plan artifacts.",
            "- Producers cannot accept their own output; verifiers cannot repair candidates.",
            f"- Visible capabilities: {', '.join(allowed) if allowed else '(none)' }.",
        )
    )

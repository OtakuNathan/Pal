from __future__ import annotations

from dataclasses import dataclass
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
    replace_internal_tool_names,
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
    memory_text_factory: Callable[[], str]
    provider_id: str = "minion.v2.worker.prompt"
    module_id: str = "minion"

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        scaffold = dict(self.scaffold_factory() or {})
        fragments: list[PromptFragment] = []

        def add(section: str, title: str, content: str, priority: int) -> None:
            text = replace_internal_tool_names(str(content or "").strip())
            if text:
                fragments.append(PromptFragment(section=section, title=title, content=text, priority=priority))

        add("identity", "Minion Identity", str(scaffold.get("identity") or ""), 10)
        add("behavior_guidance", "Role Contract", str(scaffold.get("behavior") or ""), 20)
        add("task_acceptance", "Bound Invocation", _render_bound_invocation(scaffold), 35)
        add("operating_rules", "Execution Rules", _render_execution_rules(scaffold), 60)
        add("output_contract", "Output Contract", str(scaffold.get("output_contract") or ""), 70)
        memory = str(self.memory_text_factory() or "").strip()
        if memory:
            add("memory", "Durable Worker Context", memory, 90)
        retry = str(dict(context.metadata or {}).get("retry_note") or "")
        if retry:
            add("memory", "Retry Guidance", retry, 95)
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
    if pack.acceptance_criteria:
        lines.extend(["", "## Invocation Acceptance"])
        lines.extend(f"- {item}" for item in pack.acceptance_criteria)
    references = [dict(item) for item in list(pack.workspace.get("reference_paths") or []) if isinstance(item, dict)]
    if references:
        lines.extend(["", "## Immutable Inputs"])
        if any(bool(item.get("bound_input")) and bool(item.get("required")) for item in references):
            lines.append(
                "- Every mandatory bound input must be read with op_minion_input_read before submission; ordinary filesystem reads do not record this receipt."
            )
        for item in references:
            name = str(item.get("name") or "")
            if bool(item.get("bound_input")):
                requirement = "mandatory" if bool(item.get("required")) else "optional"
                lines.append(
                    f"- {name}: {requirement} bound immutable artifact; read with op_minion_input_read(name=\"{name}\")"
                )
            else:
                lines.append(f"- {name}: declared read-only reference (truth_source={bool(item.get('truth_source'))})")
    lines.extend(
        [
            "",
            "## Boundary",
            "- This is one durable role invocation, not an autonomous workflow.",
            "- Do not create architecture, acceptance authority, hidden follow-up work, milestones, or checkpoints.",
            "- Use only visible capabilities and the bound workspace/reference roots.",
        ]
    )
    return "\n".join(lines)


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
    allowed = [str(item) for item in list(scaffold.get("allowed_capabilities") or [])]
    return "\n".join(
        (
            "- Work only on the bound role invocation.",
            "- Inspect immutable inputs before making claims or edits.",
            "- Architecture roles must use their bound architecture tools and output contract; they cannot invent alternate plan artifacts.",
            "- Producers cannot accept their own output; verifiers cannot repair candidates.",
            f"- Visible capabilities: {', '.join(allowed) if allowed else '(none)' }.",
        )
    )

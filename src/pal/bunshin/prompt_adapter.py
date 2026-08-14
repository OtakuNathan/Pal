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
    BunshinInvocationPack,
)
from pal.shared.payloads import extract_text_from_payload
from pal.shared.tool_routing import (
    TOOL_EFFICIENCY_SYSTEM_GUIDANCE,
    TOOL_ROUTING_SYSTEM_GUIDANCE,
)


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
class BunshinPromptFragmentProvider(PromptFragmentProvider):
    scaffold_factory: Callable[[], dict[str, Any]]
    role_context_factory: Callable[[], str]
    provider_id: str = "bunshin.v2.worker.prompt"
    module_id: str = "bunshin"

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

        add("identity", "Bunshin Identity", str(scaffold.get("identity") or ""), 10)
        add("tool_efficiency", "Tool Efficiency", TOOL_EFFICIENCY_SYSTEM_GUIDANCE, 15)
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
                    "block_id": "bunshin_role_working_state",
                    "raw_user_context": True,
                    "runtime_context_kind": "role_state",
                },
            )
        add("operating_rules", "Execution Rules", _render_execution_rules(scaffold), 60)
        add("tool_routing", "Tool Routing", TOOL_ROUTING_SYSTEM_GUIDANCE, 65)
        add("output_contract", "Output Contract", str(scaffold.get("output_contract") or ""), 70)
        retry = str(dict(context.metadata or {}).get("retry_note") or "")
        if retry:
            add(
                "memory",
                "Retry Guidance",
                retry,
                95,
                metadata={
                    "block_id": "bunshin_retry_guidance",
                    "raw_user_context": True,
                    "runtime_context_kind": "retry_guidance",
                },
            )
        return fragments


def build_bunshin_task_envelope(pack: BunshinInvocationPack, *, bunshin_id: str, run_id: str) -> ChannelEnvelope:
    endpoint_id = f"bunshin:{run_id}"
    return ChannelEnvelope(
        event=EventEnvelope(
            event_kind=EventKind.USER_MESSAGE,
            source_kind=SourceKind.BUNSHIN,
            payload={
                "text": render_bunshin_task_prompt(pack),
                "invocation_id": pack.invocation_id,
                "bunshin_profile": pack.bunshin_profile,
            },
            correlation_id=run_id,
        ),
        endpoint=EndpointConfig(
            endpoint_id=endpoint_id,
            channel_kind="stdio",
            binding_key=run_id,
            send_policy={"route": "bunshin_manager"},
        ),
        response_handle=ResponseHandle(
            endpoint_id=endpoint_id,
            reply_target={"run_id": run_id, "bunshin_id": bunshin_id, "invocation_id": pack.invocation_id},
        ),
    )


def bunshin_primary_input(envelope: ChannelEnvelope) -> str:
    return extract_text_from_payload(envelope.event.payload).strip() or "No worker instruction was provided. Report blocked."


def render_bunshin_task_prompt(pack: BunshinInvocationPack) -> str:
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
    repo_path = str(pack.workspace.get("repo_path") or "").strip()
    if repo_path:
        lines.extend(
            [
                "",
                "## Workspace Root",
                f"- Product workspace: `{repo_path}`",
                (
                    "- Shell tools start in that workspace. Keep product-code, "
                    "build, test, and Git operations there (or use paths relative "
                    "to it)."
                ),
                (
                    "- `/pal` is only the projection root for immutable "
                    "references; it is not the product repository. Never `cd "
                    "/pal` to inspect product code or run Git."
                ),
            ]
        )
    architect_path = str(pack.workspace.get("architect_path") or "").strip()
    if architect_path:
        read_args = json.dumps(
            {"file_path": architect_path},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        lines.extend(
            [
                "",
                "## Writable Outputs",
                (
                    "- architect.yaml: Manager-preseeded writable contract; "
                    f"path={architect_path}; read_file_args={read_args}"
                ),
                (
                    "- Read and edit that exact existing file. Reuse the same "
                    "file_path for edit_file or write_file; do not guess, create, "
                    "or search for another architect.yaml."
                ),
            ]
        )
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
                "## Reference Access Efficiency",
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


def _initial_skill_reminders(pack: BunshinInvocationPack) -> list[str]:
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


def _execution_discipline_lines(pack: BunshinInvocationPack) -> list[str]:
    lines = [
        "- Preserve contract correctness and role boundaries. Efficiency means eliminating duplicate work, never skipping decisive evidence.",
        "- Make one bounded pass over the owned scope and only the evidence needed for this role. Once the next action is clear, act; do not reopen settled questions unless new evidence contradicts them.",
        "- Never repeat an operation when the tool, arguments, relevant state, and observed error are unchanged. First use the returned error, retry directive, and affordances to change the input or state; if no meaningful change is available, record the blocker or finding and stop. A rejection may be retried only after the relevant input or state actually changes; if the same rejection fingerprint recurs, stop and report the blocker instead of probing around it. retry=safe permits a corrected retry, not an unchanged replay; effect=unknown requires reconciliation before retry.",
        "- Prefer the smallest contract-complete action. Do not add optional abstraction, evidence, or polish after this role's completion conditions are satisfied.",
    ]
    role = str(dict(dict(pack.metadata or {}).get("bunshin_v2") or {}).get("role") or "")
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
            "- Architecture: after one requirements-consistency pass, declare the smallest complete module and contract graph. Do not rehearse implementation; call contract_submit as soon as the declared completion conditions hold."
        )
    elif role == "reviewer":
        lines.append(
            "- Review: perform one breadth-first semantic pass, batch all material findings, and submit once. Do not reopen accepted surfaces unless current evidence contradicts them."
        )
    return lines


def prompt_view_from_pack(pack: BunshinInvocationPack) -> dict[str, Any]:
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

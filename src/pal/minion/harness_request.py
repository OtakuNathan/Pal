from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pal.shared import MinionInvocationPack


_PAL_TOOL_TOKENS = (
    "ask_question",
    "contract_submit",
    "update_checklist",
    "read_tool",
    "call_tool",
)


@dataclass(frozen=True)
class ArchitectHarnessRequest:
    developer_instructions: str
    user_input: str
    cwd: Path
    architect_path: Path
    work_item_seed: tuple[dict[str, Any], ...]


def architect_harness_assignment_fingerprint(
    pack: MinionInvocationPack,
) -> str:
    binding = dict(dict(pack.metadata or {}).get("minion_v2") or {})
    fingerprint = str(
        binding.get("authoring_input_fingerprint") or ""
    ).strip()
    if not fingerprint:
        raise ValueError(
            "Architect harness requires an authoring input fingerprint"
        )
    return fingerprint


def compile_architect_harness_request(
    pack: MinionInvocationPack,
) -> ArchitectHarnessRequest:
    binding = dict(dict(pack.metadata or {}).get("minion_v2") or {})
    if str(binding.get("role") or "") != "architect":
        raise ValueError("Codex Architect harness only accepts architect packs")
    profile = dict(pack.resolved_profile or {})
    behavior = str(profile.get("behavior_fragment") or "").strip()
    output_contract = str(
        profile.get("output_contract_fragment") or ""
    ).strip()
    combined = "\n\n".join(
        part for part in (behavior, output_contract) if part
    )
    forbidden = [
        token for token in _PAL_TOOL_TOKENS if token in combined
    ]
    if forbidden:
        raise ValueError(
            "harness-neutral Architect profile contains Pal tool names: "
            + ", ".join(forbidden)
        )
    workspace = dict(pack.workspace or {})
    architect_text = str(workspace.get("architect_path") or "").strip()
    if not architect_text:
        raise ValueError("Architect harness requires a bound architect_path")
    architect_path = Path(architect_text).expanduser().resolve()
    cwd = _workspace_root(workspace)
    playbook = dict(binding.get("role_protocol") or {}).get("playbook")
    steps = [
        dict(item)
        for item in list(dict(playbook or {}).get("steps") or [])
        if isinstance(item, Mapping)
    ]
    seed = tuple(
        dict(item)
        for item in list(binding.get("work_item_seed") or [])
        if isinstance(item, Mapping)
    )
    developer_instructions = "\n\n".join(
        (
            "Execute the supplied architecture contract directly. "
            "Design only module boundaries, public declarations, ownership, "
            "lifecycle, invariants, state machines when needed, dependency "
            "handoffs, and end-to-end contract flows. Never implement product "
            "behavior. The Manager owns Git lifecycle, task revisions, "
            "checklist storage, validation, and submission.",
            combined,
            "Use native planning as a compact execution cursor. Preserve the "
            "fixed phase names exactly and update them as work advances. "
            "Use native user input only for one decisive question at a time. "
            "Write the bound files directly and finish with a short response; "
            "the response is not the deliverable.",
        )
    )
    lines = [
        "# Architect Assignment",
        "",
        "Read the immutable task ledger, perform one bounded consistency "
        "pass, and author the smallest complete module-level architecture in "
        "the bound workspace. Define responsibilities, directional public "
        "contracts and dependency handoffs, ownership, lifecycle, invariants, "
        "observable errors, state machines only where needed, and meaningful "
        "end-to-end contract flows. Write declaration skeletons without "
        "product behavior, then encode and reconcile the same design in the "
        "bound architect.yaml. Do not compile, build, test, link, commit, or "
        "implement private algorithms.",
        "",
        "## Acceptance",
        "- Boundaries and responsibilities are declared.",
        "- Every state, worker, object, and resource has exactly one owner.",
        "- Public contracts, errors, lifecycle transitions, and composition "
        "joins are closed.",
        "- Private implementation is explicitly deferred.",
        "- Every fixed native-plan item is completed and the bound files agree "
        "before the final response.",
    ]
    if steps:
        lines.extend(["", "## Fixed Plan Phases"])
        for item in steps:
            key = str(item.get("key") or "").replace("_", " ").strip()
            instruction = str(item.get("instruction") or "").strip()
            done_when = str(item.get("done_when") or "").strip()
            lines.append(
                f"- `{key}`: {instruction} Done when: {done_when}"
            )
    routed_items = [
        dict(item)
        for item in seed
        if str(item.get("kind") or "") != "phase"
        and str(item.get("summary") or "").strip()
    ]
    if routed_items:
        lines.extend(
            [
                "",
                "## Manager-Routed Work Items",
                "Read the corresponding Manager-bound immutable finding "
                "inputs before changing files.",
                "Include each exact item below in the native plan and complete "
                "it before finishing:",
                *[
                    f"- `{str(item['summary']).strip()}`"
                    for item in routed_items
                ],
            ]
        )
    lines.extend(
        [
            "",
            "## Bound Output",
            f"- architect.yaml: `{architect_path}`",
            "- Existing declaration files in the workspace may be created or "
            "edited only as declaration skeletons required by the contract.",
            "- Do not commit, branch, merge, or alter Manager-owned task files.",
        ]
    )
    references = [
        dict(item)
        for item in list(workspace.get("reference_paths") or [])
        if isinstance(item, Mapping)
    ]
    if references:
        lines.extend(["", "## Immutable Inputs"])
        for item in references:
            path = str(item.get("path") or "").strip()
            if not path:
                continue
            lines.append(
                f"- {str(item.get('name') or 'reference')}: `{path}`"
            )
    reminders = [
        str(dict(item).get("system_reminder") or "").strip()
        for item in list(
            dict(pack.metadata or {}).get("initial_skill_injections") or []
        )
        if isinstance(item, Mapping)
        and str(dict(item).get("system_reminder") or "").strip()
    ]
    if reminders:
        lines.extend(["", "## Approved Operating Manuals", *reminders])
    request = ArchitectHarnessRequest(
        developer_instructions=developer_instructions,
        user_input="\n".join(lines).strip(),
        cwd=cwd,
        architect_path=architect_path,
        work_item_seed=seed,
    )
    leaked = [
        token
        for token in _PAL_TOOL_TOKENS
        if token in (
            request.developer_instructions + "\n" + request.user_input
        )
    ]
    if leaked:
        raise ValueError(
            "Architect harness request leaked Pal tool names: "
            + ", ".join(leaked)
        )
    return request


def _workspace_root(workspace: Mapping[str, Any]) -> Path:
    for key in (
        "repo_path",
        "worktree_path",
        "workspace_path",
        "root",
        "path",
    ):
        value = str(workspace.get(key) or "").strip()
        if value:
            return Path(value).expanduser().resolve()
    raise ValueError("Architect harness workspace has no repository root")

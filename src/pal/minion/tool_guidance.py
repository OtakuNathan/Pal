from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping

from pal.execution.tool_facade import ToolGuidance


TOOL_GUIDANCE_FIELDS = (
    "purpose",
    "use_when",
    "do_not_use_when",
    "failure_next_steps",
)


MINION_SYSTEM_TOOL_GUIDANCE_OVERRIDES = MappingProxyType(
    {
        "op_exec_shell": MappingProxyType(
            {
                "use_when": (
                    "Use bounded workspace discovery and repository search with standard read-only commands such as "
                    "rg --files, rg, find, grep, and ls when structure or matching locations are needed. Also use shell "
                    "for tests, builds, scripts, package commands, process inspection, and runtime probes required by "
                    "your assigned task. The assigned worktree is already the default working directory, so do not guess "
                    "or reconstruct a role-workspace path. Put generated build output in $PAL_BUILD_SCRATCH when the "
                    "worktree is read-only. Ordinary file creation and deletion inside a writable assigned worktree are allowed. "
                    "Run long-lived tests and builds directly so their complete stdout and stderr remain available."
                ),
                "do_not_use_when": (
                    "Stay focused on your assigned task; do not inspect or manipulate runtime, orchestration, capability, "
                    "or workflow state. Once an exact file is known, call read_file instead of shelling out to cat, head, "
                    "tail, or sed. Prefer edit_file for precise text replacement and write_file for complete text files "
                    "once the exact path and content are known. Git is available here only for classified read-only inspection such as "
                    "status, diff, log, show, blame, grep, ls-files, rev-parse, show-ref, and non-mutating branch queries. "
                    "Git mutations and unknown Git subcommands are trapped; leave repository checkpoint mutations to the "
                    "Manager. Do not use shell networking or package-download commands; use search_web/read_web for web "
                    "research and report missing dependencies as an environment blocker. Do not pipe long-running tests "
                    "or builds through head, tail, or grep merely to shorten output; result budgeting handles large output, "
                    "while those pipelines hide the command that is stalled."
                ),
                "failure_next_steps": (
                    "If a command is trapped, do not retry it through another shell spelling or wrapper. For a trapped "
                    "Git command, use a permitted read-only subcommand or stop. For an ordinary command failure, inspect "
                    "stdout, stderr, and exit status, correct the command or environment, and then rerun only when safe."
                ),
            }
        )
    }
)


def normalize_tool_guidance_patch(value: object, *, context: str = "tool guidance") -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{context} must be an object")
    unknown = sorted(set(str(key) for key in value) - set(TOOL_GUIDANCE_FIELDS))
    if unknown:
        raise ValueError(f"{context} has unknown fields: {', '.join(unknown)}")
    patch: dict[str, str] = {}
    for field in TOOL_GUIDANCE_FIELDS:
        if field not in value:
            continue
        raw = value[field]
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"{context}.{field} must be a non-empty string")
        patch[field] = raw.strip()
    if not patch:
        raise ValueError(f"{context} must set at least one non-empty guidance field")
    return patch


def normalize_tool_guidance_overrides(value: object) -> dict[str, dict[str, str]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("capability_guidance_overrides must be an object")
    overrides: dict[str, dict[str, str]] = {}
    for raw_canonical, patch in value.items():
        canonical = str(raw_canonical).strip()
        if not canonical:
            raise ValueError("capability_guidance_overrides keys must be non-empty")
        overrides[canonical] = normalize_tool_guidance_patch(
            patch,
            context=f"capability_guidance_overrides[{canonical!r}]",
        )
    return overrides


def merge_tool_guidance_overrides(
    base: object,
    extra: object,
) -> dict[str, dict[str, str]]:
    merged = normalize_tool_guidance_overrides(base)
    for canonical, patch in normalize_tool_guidance_overrides(extra).items():
        merged[canonical] = {**merged.get(canonical, {}), **patch}
    return merged


def minion_tool_guidance(
    canonical_path: str,
    guidance: ToolGuidance,
    role_patch: Mapping[str, Any] | None = None,
) -> ToolGuidance:
    values = guidance.model_dump(mode="python")
    if role_patch:
        values.update(
            normalize_tool_guidance_patch(
                role_patch,
                context=f"capability_guidance_overrides[{canonical_path!r}]",
            )
        )
    system_patch = MINION_SYSTEM_TOOL_GUIDANCE_OVERRIDES.get(str(canonical_path or "").strip())
    if system_patch:
        # System guidance is applied last so role/profile configuration cannot
        # weaken sandbox routing or recovery rules.
        values.update(dict(system_patch))
    return ToolGuidance.model_validate(values, strict=True)


__all__ = [
    "MINION_SYSTEM_TOOL_GUIDANCE_OVERRIDES",
    "TOOL_GUIDANCE_FIELDS",
    "merge_tool_guidance_overrides",
    "minion_tool_guidance",
    "normalize_tool_guidance_overrides",
    "normalize_tool_guidance_patch",
]

from __future__ import annotations
import pal.bunshin.turns as _dependency_turns
import re
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.skill_context import normalized_skill_injection
from pal.bunshin.sandbox import _project_sandbox_references, with_bunshin_sandbox_metadata
from pal.bunshin.adapters import prepare_role_workspace
from pal.bunshin.contracts import PermanentEffectError, SubmissionInvariantError
from pal.bunshin.input_binding import declared_inputs_from_references
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.workspace_tools import _safe_reference_name
from pal.shared import BunshinInvocationPack
from pal.bunshin.semantic_orchestration.role_inputs import EPHEMERAL_ROLE_INPUT_NAMES
from pal.bunshin.semantic_orchestration.callbacks import SkillInjector
from pal.bunshin.semantic_orchestration.role_inputs import _allocate_declared_input_references, _attach_bound_input_read_only_overlays, _role_uses_bound_durable_workspace


def _workflow_skill_injections(
    request: Mapping[str, Any],
    inject_skill: SkillInjector | None,
) -> list[dict[str, str]]:
    skill_refs = [
        str(item or "").strip()
        for item in list(request.get("skill_refs") or [])
        if str(item or "").strip()
    ]
    if skill_refs and inject_skill is None:
        raise PermanentEffectError("approved skill injection is unavailable in Manager")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for skill_ref in skill_refs:
        if skill_ref in seen:
            continue
        seen.add(skill_ref)
        try:
            injected = dict(inject_skill(skill_ref)) if inject_skill is not None else {}
        except Exception as exc:
            raise PermanentEffectError(
                f"approved skill injection failed: {skill_ref} ({exc})"
            ) from exc
        skill_id = str(injected.get("skill_id") or skill_ref).strip()
        normalized = normalized_skill_injection(injected)
        if normalized is None:
            raise PermanentEffectError(
                f"approved skill produced no valid user context: {skill_ref}"
            )
        result.append({"skill_id": skill_id, "user_context": normalized["user_context"]})
    return result


def _prepare_role_workspace_before_environment(
    runtime_root: Path,
    workspace: Mapping[str, Any],
    *,
    role: str,
    invocation_id: str,
    run_id: str,
    fencing_token: int,
    prepare_workspace: bool,
) -> tuple[dict[str, Any], bool]:
    prepared = dict(workspace or {})
    uses_bound_durable_workspace = _role_uses_bound_durable_workspace(role, prepared)
    if not prepare_workspace or uses_bound_durable_workspace:
        return prepared, uses_bound_durable_workspace
    role_pack = prepare_role_workspace(
        runtime_root,
        BunshinInvocationPack(
            invocation_id=invocation_id,
            workspace=prepared,
        ),
        run_id=run_id,
        attempt_key=f"fence-{fencing_token}",
    )
    return dict(role_pack.workspace), uses_bound_durable_workspace


def _bind_role_attempt_sandbox(
    runtime_root: Path,
    pack: BunshinInvocationPack,
    *,
    run_id: str,
    durable_prompt_reused: bool,
) -> BunshinInvocationPack:
    """Finalize one attempt without losing a durable prompt's sandbox binds.

    A persisted prompt already contains Manager-produced host-to-sandbox
    reference bindings and projected ``/pal/references`` paths.  Sanitizing it
    again would discard the bindings while leaving the projected paths behind,
    so a retry would mistake those sandbox paths for host sources.  Fresh packs
    still pass through the normal manager-metadata sanitizer.
    """

    if durable_prompt_reused:
        if not isinstance(dict(pack.metadata or {}).get("sandbox"), dict):
            raise SubmissionInvariantError(
                "durable role prompt lost its sandbox reference binding"
            )
    else:
        pack = _dependency_turns.sanitize_runner_session_pack(pack)
    return with_bunshin_sandbox_metadata(runtime_root, pack, run_id=run_id)


def _refresh_ephemeral_role_reference_binds(
    durable_pack: BunshinInvocationPack,
    current_pack: BunshinInvocationPack,
) -> BunshinInvocationPack:
    """Point attempt-local durable prompt binds at the current attempt inputs.

    Assignment retries intentionally reuse the original semantic prompt, but
    ``workspace_preparation`` describes one concrete process attempt and is
    excluded from the assignment fingerprint.  Reusing its old host bind makes
    the projected reference advertise a new fence while still exposing the
    previous fence's scratch paths inside the sandbox.
    """

    # File references must use the same directory bind and filename include
    # as their first-attempt projection.
    from pal.bunshin.workspace_tools import _normalized_reference_paths

    current_references = {
        str(item.get("name") or ""): dict(item)
        for item in _normalized_reference_paths(dict(current_pack.workspace or {}))
        if isinstance(item, Mapping)
        and str(item.get("name") or "") in EPHEMERAL_ROLE_INPUT_NAMES
        and not bool(item.get("bound_input"))
        and not item.get("declared_input_name")
    }
    if not current_references:
        return durable_pack

    value = durable_pack.to_dict()
    metadata = dict(value.get("metadata") or {})
    sandbox = dict(metadata.get("sandbox") or {})
    binds = [
        dict(item)
        for item in list(sandbox.get("reference_binds") or [])
        if isinstance(item, Mapping)
    ]
    refreshed: set[str] = set()
    for bind in binds:
        name = str(bind.get("name") or "")
        reference = current_references.get(name)
        if reference is None:
            continue
        bind.update(
            {
                "source_path": str(reference.get("path") or ""),
                "include": list(reference.get("include") or []),
                "required": bool(reference.get("required", True)),
            }
        )
        refreshed.add(name)
    missing = sorted(set(current_references) - refreshed)
    if missing:
        raise SubmissionInvariantError(
            "durable role prompt lost attempt-local sandbox reference bindings: "
            + ", ".join(missing)
        )
    sandbox["reference_binds"] = binds
    metadata["sandbox"] = sandbox
    brief = dict(metadata.get("requirements_brief") or {})
    current_brief = dict(current_pack.metadata.get("requirements_brief") or {})
    latest = {str(item.get("name") or ""): item for item in current_brief.get("references") or []
              if isinstance(item, Mapping) and item.get("name") in refreshed and not item.get("declared_input_name")}
    if "references" in brief:
        brief["references"] = [latest.get(str(item.get("name") or ""), item)
                               for item in brief.get("references") or [] if isinstance(item, Mapping)]
        metadata["requirements_brief"] = brief
    return BunshinInvocationPack.from_dict({**value, "metadata": metadata})


def _refresh_verified_workspace_inputs(
    durable_pack: BunshinInvocationPack, current_pack: BunshinInvocationPack, *,
    bound_entries: list[dict[str, Any]], manager_input_names: tuple[str, ...] = (),
) -> BunshinInvocationPack:
    """Keep durable semantic policy, but use the verified input projections."""
    value = durable_pack.to_dict()
    workspace = dict(value.get("workspace") or {})
    metadata = dict(value.get("metadata") or {})
    sandbox = dict(metadata.get("sandbox") or {})
    old_refs = [dict(item) for item in workspace.get("reference_paths") or [] if isinstance(item, Mapping)]
    old_binds = [dict(item) for item in sandbox.get("reference_binds") or [] if isinstance(item, Mapping)]
    brief = dict(metadata.get("requirements_brief") or {})
    brief_refs = [dict(item) for item in brief.get("references") or [] if isinstance(item, Mapping)]
    manager_names = {_safe_reference_name(name) for name in manager_input_names}
    declared_names = {str(item.get("name") or "") for item in [*old_refs, *brief_refs]
                      if item.get("bound_input") or item.get("declared_input_name")}
    # Pre-marker recovered inputs used an ordinary external bind. Recognize
    # that legacy projection by its actual frozen source, never by name alone.
    _, raw_binds = _project_sandbox_references({"reference_paths": bound_entries})
    for bind in old_binds:
        name = str(bind.get("name") or "")
        if name not in manager_names and any(
            item["name"] == name and item["source_path"] == bind.get("source_path")
            and item["include"] == bind.get("include") for item in raw_binds
        ):
            declared_names.add(name)
    if declared_names & manager_names:
        # An older broken projection may already have overwritten a pinned
        # semantic input. Do not silently fabricate it from new role inputs.
        raise SubmissionInvariantError("durable role prompt has a conflicting Manager input identity")
    semantic_refs = [item for item in old_refs if item.get("name") not in declared_names]
    semantic_binds = [item for item in old_binds if item.get("name") not in declared_names]
    semantic_brief = [item for item in brief_refs if item.get("name") not in declared_names]
    reserved = manager_names | {str(item.get("name") or "") for item in [*semantic_refs, *semantic_binds, *semantic_brief]}
    allocated = _allocate_declared_input_references(reserved, bound_entries)
    projected, binds = _project_sandbox_references({"reference_paths": allocated})
    workspace["reference_paths"] = semantic_refs + projected["reference_paths"]
    sandbox["reference_binds"] = semantic_binds + binds
    if "sandbox" in metadata:
        metadata["sandbox"] = sandbox
    if "references" in brief or allocated:
        brief["references"] = semantic_brief + allocated
        metadata["requirements_brief"] = brief
    _attach_bound_input_read_only_overlays(workspace, [item for item in bound_entries if item.get("bound_input")])
    # These fields describe this preparation, not the original semantic task.
    for key in ("languages", "primary_language", "prepared_environment_fingerprint",
                "lsp_environment_fingerprint"):
        if key in current_pack.workspace:
            workspace[key] = current_pack.workspace[key]
        else:
            workspace.pop(key, None)
    return BunshinInvocationPack.from_dict({**value, "workspace": workspace, "metadata": metadata})


def _reconcile_declared_input_reference_binds(
    durable_pack: BunshinInvocationPack, current_pack: BunshinInvocationPack, *, request: Mapping[str, Any],
) -> BunshinInvocationPack:
    """Remove legacy host aliases that competed with declared frozen inputs."""
    aliases = {
        _safe_reference_name(f"user_{item.name}")
        for item in declared_inputs_from_references(request.get("references") or [])
    }
    if not aliases:
        return durable_pack
    value = durable_pack.to_dict()
    workspace = dict(value.get("workspace") or {})
    metadata = dict(value.get("metadata") or {})
    sandbox = dict(metadata.get("sandbox") or {})
    references = [dict(item) for item in workspace.get("reference_paths") or [] if isinstance(item, Mapping)]
    binds = [dict(item) for item in sandbox.get("reference_binds") or [] if isinstance(item, Mapping)]
    brief = dict(metadata.get("requirements_brief") or {})
    brief_refs = [dict(item) for item in brief.get("references") or [] if isinstance(item, Mapping)]
    previous_brief = {_safe_reference_name(str(item.get("name") or "")): item for item in brief_refs}
    old_aliases = {
        str(item.get("name") or "") for item in references if not item.get("bound_input")
    } | {str(item.get("name") or "") for item in binds} | set(previous_brief)
    aliases &= old_aliases
    if not aliases:
        return durable_pack

    # A real absolute reference can share the normalized alias. In that case
    # keep the currently authorized host binding instead of the old one.
    current_workspace, current_binds = _project_sandbox_references(dict(current_pack.workspace or {}))
    current_by_name = {str(item["name"]): item for item in current_binds}
    previous_by_name = {str(item.get("name") or ""): item for item in binds}
    current_brief = dict(current_pack.metadata.get("requirements_brief") or {})
    current_brief_by_name = {_safe_reference_name(str(item.get("name") or "")): dict(item)
                             for item in current_brief.get("references") or [] if isinstance(item, Mapping)}
    changed = {
        name for name in aliases if name not in current_by_name
        or previous_by_name.get(name) != current_by_name[name]
        or (name in previous_brief and previous_brief[name] != current_brief_by_name.get(name))
    }
    if not changed:
        return durable_pack
    kept_references = [item for item in references if item.get("bound_input") or item.get("name") not in changed]
    kept_names = {item.get("name") for item in kept_references}
    workspace["reference_paths"] = kept_references + [
        item for item in current_workspace["reference_paths"]
        if item.get("name") in changed and item.get("name") not in kept_names
    ]
    sandbox["reference_binds"] = [item for item in binds if item.get("name") not in changed] + [
        item for item in current_binds if item.get("name") in changed
    ]
    metadata["sandbox"] = sandbox
    correction = (
        "Legacy input reference bindings changed on recovery: " + ", ".join(sorted(changed)) + ". "
        "Use the current immutable input bindings, discard earlier observations from the old aliases, "
        "and recheck any work or decisions based on those observations."
    )
    acceptance = list(value.get("acceptance_criteria") or [])
    if correction not in acceptance:
        acceptance.append(correction)
    if "references" in brief:
        brief["references"] = [
            item for item in brief_refs if _safe_reference_name(str(item.get("name") or "")) not in changed
        ] + [item for name, item in current_brief_by_name.items() if name in changed and name in current_by_name]
    if brief.get("acceptance_criteria"):
        brief_acceptance = list(brief["acceptance_criteria"])
        if correction not in brief_acceptance:
            brief_acceptance.append(correction)
        brief["acceptance_criteria"] = brief_acceptance
    if brief:
        metadata["requirements_brief"] = brief
    return BunshinInvocationPack.from_dict({**value, "workspace": workspace, "metadata": metadata,
                                            "acceptance_criteria": acceptance})


def _workspace_tooling_from_work_view(
    work_view: Mapping[str, Any],
) -> dict[str, Any]:
    """Translate explicit accepted language context into workspace tooling."""

    context = dict(work_view.get("context") or {})
    primary_language = _canonical_lsp_language(
        str(context.get("primary_language") or context.get("language") or "")
    )
    languages = [
        language
        for language in (
            _canonical_lsp_language(str(value))
            for value in list(context.get("languages") or [])
        )
        if language
    ]
    if primary_language:
        languages.insert(0, primary_language)
    languages = list(dict.fromkeys(languages))
    tooling: dict[str, Any] = {}
    if primary_language:
        tooling.update(
            {
                "primary_language": primary_language,
                "languages": languages,
            }
        )
    explicit_standard = str(context.get("cpp_standard") or "").strip().lower()
    if re.fullmatch(
        r"(?:c\+\+(?:98|03|11|14|17|20|23|26)|c(?:89|90|99|11|17|18|23))",
        explicit_standard,
    ):
        tooling["cpp_standard"] = explicit_standard
        return tooling
    language = str(context.get("language") or "").strip().lower()
    cpp_match = re.fullmatch(r"c\+\+\s*(98|03|11|14|17|20|23|26)", language)
    if cpp_match:
        tooling["cpp_standard"] = f"c++{cpp_match.group(1)}"
        return tooling
    c_match = re.fullmatch(r"c\s*(89|90|99|11|17|18|23)", language)
    if c_match:
        tooling["cpp_standard"] = f"c{c_match.group(1)}"
    return tooling


def _canonical_lsp_language(value: str) -> str:
    """Map an accepted human language label to Pal's canonical LSP id."""

    normalized = " ".join(str(value or "").strip().lower().split())
    if not normalized:
        return ""
    if re.fullmatch(r"objective[ -]?c\+\+(?:\s*\d+)?", normalized):
        return "objective-cpp"
    if re.fullmatch(r"objective[ -]?c(?:\s*\d+)?", normalized):
        return "objective-c"
    if re.fullmatch(r"c\+\+(?:\s*(?:98|03|11|14|17|20|23|26))?", normalized):
        return "cpp"
    if re.fullmatch(r"c(?:\s*(?:89|90|99|11|17|18|23))?", normalized):
        return "c"
    without_version = re.sub(
        r"\s+(?:v(?:ersion)?\s*)?\d+(?:\.\d+)*(?:\s+(?:lts|edition))?$",
        "",
        normalized,
    )
    aliases = {
        "bash": "shell",
        "c#": "csharp",
        "c-sharp": "csharp",
        "cs": "csharp",
        "csharp": "csharp",
        "cpp": "cpp",
        "cxx": "cpp",
        "css": "css",
        "ecmascript": "javascript",
        "go": "go",
        "golang": "go",
        "html": "html",
        "java": "java",
        "javascript": "javascript",
        "js": "javascript",
        "jsx": "javascript",
        "json": "json",
        "lua": "lua",
        "objective-c": "objective-c",
        "objective-cpp": "objective-cpp",
        "python": "python",
        "py": "python",
        "rs": "rust",
        "rust": "rust",
        "sh": "shell",
        "shell": "shell",
        "shellscript": "shell",
        "typescript": "typescript",
        "ts": "typescript",
        "tsx": "typescript",
        "yaml": "yaml",
        "yml": "yaml",
    }
    return aliases.get(without_version, "")


def _role_submission_kind(activation: RoleActivation, *, contract_authoring: bool) -> str:
    del contract_authoring
    if activation.role == OrchestrationRole.ARCHITECT:
        return "contract"
    if activation.role == OrchestrationRole.REVIEWER:
        return (
            "architecture_review"
            if activation.mode == RoleMode.ARCHITECTURE
            else "standalone_review"
        )
    if activation.role == OrchestrationRole.IMPLEMENTATION:
        return "candidate"
    if activation.role == OrchestrationRole.VERIFIER:
        return "verification"
    raise ValueError(f"unsupported role activation: {activation.to_dict()}")

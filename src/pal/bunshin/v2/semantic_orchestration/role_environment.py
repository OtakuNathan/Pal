from __future__ import annotations
import pal.bunshin.turns as _dependency_turns
import re
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.skill_context import normalized_skill_injection
from pal.bunshin.harnesses import PAL_HARNESS_ID, BunshinHarnessRegistryGeneration, BunshinHarnessSpec
from pal.bunshin.sandbox import with_bunshin_sandbox_metadata
from pal.bunshin.turns import sanitize_runner_session_pack
from pal.bunshin.v2.adapters import prepare_v2_role_workspace
from pal.bunshin.v2.contracts import PermanentEffectError, SubmissionInvariantError
from pal.bunshin.v2.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.shared import BunshinInvocationPack
from pal.bunshin.v2.semantic_orchestration.role_inputs import EPHEMERAL_ROLE_INPUT_NAMES
from pal.bunshin.v2.semantic_orchestration.callbacks import SkillInjector
from pal.bunshin.v2.semantic_orchestration.role_inputs import _role_uses_bound_durable_workspace


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
    role_pack = prepare_v2_role_workspace(
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

    current_references = {
        str(item.get("name") or ""): dict(item)
        for item in list(dict(current_pack.workspace or {}).get("reference_paths") or [])
        if isinstance(item, Mapping)
        and str(item.get("name") or "") in EPHEMERAL_ROLE_INPUT_NAMES
        and not bool(item.get("bound_input"))
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
    return BunshinInvocationPack.from_dict({**value, "metadata": metadata})


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


def _select_attempt_harness(
    generation: BunshinHarnessRegistryGeneration,
    *,
    role: str,
    prior_attempts: tuple[Mapping[str, Any], ...] = (),
) -> BunshinHarnessSpec:
    preferred = generation.select(role)
    if preferred.harness_id == PAL_HARNESS_ID:
        return preferred
    failed_preferred = [
        attempt
        for attempt in prior_attempts
        if str(attempt.get("harness_id") or "") == preferred.harness_id
        and str(attempt.get("status") or "")
        in {"failed", "interrupted", "cancelled", "lost"}
    ]
    if len(failed_preferred) < 2:
        return preferred
    return next(
        spec
        for spec in generation.specs
        if spec.harness_id == PAL_HARNESS_ID and spec.supports(role)
    )

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from pal.lsp.config import load_builtin_lsp_templates, load_lsp_server_file, lsp_config_root
from pal.shared import TaskContextPack


LANGUAGE_ALIASES = {
    "bash": "shell",
    "c": "c",
    "c++": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "cpp": "cpp",
    "css": "css",
    "go": "go",
    "golang": "go",
    "html": "html",
    "javascript": "javascript",
    "js": "javascript",
    "json": "json",
    "objective-c": "objc",
    "objective-c++": "objcpp",
    "objective-cpp": "objcpp",
    "objc": "objc",
    "objcpp": "objcpp",
    "py": "python",
    "python": "python",
    "rs": "rust",
    "rust": "rust",
    "sh": "shell",
    "shell": "shell",
    "ts": "typescript",
    "typescript": "typescript",
    "yaml": "yaml",
    "yml": "yaml",
}

LANGUAGE_EXTENSIONS = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".css": "css",
    ".go": "go",
    ".h": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".html": "html",
    ".htm": "html",
    ".hxx": "cpp",
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".m": "objc",
    ".mm": "objcpp",
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".sh": "shell",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".yaml": "yaml",
    ".yml": "yaml",
}


@dataclass(frozen=True)
class WorkspaceEnvironmentContext:
    repo_path: Path
    languages: tuple[str, ...]
    write_files: bool
    available_lsp_server_ids: frozenset[str]


@dataclass
class WorkspaceEnvironmentPatch:
    server_ids: list[str] = field(default_factory=list)
    created_files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def merge(self, other: "WorkspaceEnvironmentPatch") -> None:
        for server_id in other.server_ids:
            if server_id not in self.server_ids:
                self.server_ids.append(server_id)
        for path in other.created_files:
            if path not in self.created_files:
                self.created_files.append(path)
        self.skipped.extend(other.skipped)


class WorkspaceEnvironmentPreparer(Protocol):
    language_ids: tuple[str, ...]
    required_lsp_server_ids: tuple[str, ...]

    def prepare(self, context: WorkspaceEnvironmentContext) -> WorkspaceEnvironmentPatch:
        ...


class PythonEnvironmentPreparer:
    language_ids = ("python",)
    required_lsp_server_ids = ("pyright",)

    def prepare(self, context: WorkspaceEnvironmentContext) -> WorkspaceEnvironmentPatch:
        return WorkspaceEnvironmentPatch(
            server_ids=["pyright"],
            skipped=["python uses the pyright sidecar; no repo config required"],
        )


class ClangdEnvironmentPreparer:
    language_ids = ("c", "cpp", "objc", "objcpp")
    required_lsp_server_ids = ("clangd",)

    def prepare(self, context: WorkspaceEnvironmentContext) -> WorkspaceEnvironmentPatch:
        patch = WorkspaceEnvironmentPatch(server_ids=["clangd"])
        if not context.write_files:
            patch.skipped.append("repo already existed; did not create baseline clangd config")
            return patch
        clangd_config = context.repo_path / ".clangd"
        compile_commands = context.repo_path / "compile_commands.json"
        if clangd_config.exists():
            patch.skipped.append(".clangd already exists")
            return patch
        if compile_commands.exists():
            patch.skipped.append("compile_commands.json already exists")
            return patch
        clangd_config.write_text(_default_clangd_config(context.languages), encoding="utf-8")
        patch.created_files.append(".clangd")
        return patch


PREPARERS: tuple[WorkspaceEnvironmentPreparer, ...] = (
    PythonEnvironmentPreparer(),
    ClangdEnvironmentPreparer(),
)


def prepare_workspace_environment(
    repo_path: Path,
    pack: TaskContextPack,
    workspace: dict[str, Any],
    *,
    write_files: bool,
    runtime_root: Path | None = None,
    preparers: tuple[WorkspaceEnvironmentPreparer, ...] = PREPARERS,
    available_lsp_server_ids: set[str] | None = None,
) -> dict[str, Any]:
    languages = workspace_languages(pack, workspace)
    if not languages:
        return {}
    available_lsp_configs = _available_lsp_configs(runtime_root) if available_lsp_server_ids is None else {}
    available_servers = frozenset(
        available_lsp_server_ids if available_lsp_server_ids is not None else set(available_lsp_configs)
    )
    context = WorkspaceEnvironmentContext(
        repo_path=Path(repo_path),
        languages=tuple(languages),
        write_files=bool(write_files),
        available_lsp_server_ids=available_servers,
    )
    combined = WorkspaceEnvironmentPatch()
    handled_languages: set[str] = set()
    for preparer in preparers:
        matched_languages = set(preparer.language_ids).intersection(languages)
        if not matched_languages:
            continue
        handled_languages.update(matched_languages)
        missing = [server_id for server_id in preparer.required_lsp_server_ids if server_id not in available_servers]
        if missing:
            combined.skipped.append(
                f"{'/'.join(preparer.language_ids)} skipped; missing LSP server template(s): {', '.join(missing)}"
            )
            continue
        combined.merge(preparer.prepare(context))
    if available_lsp_configs:
        _add_template_backed_lsp_servers(
            combined,
            languages=[language for language in languages if language not in handled_languages],
            available_lsp_configs=available_lsp_configs,
        )
    return {
        "languages": languages,
        "lsp_setup": {
            "languages": languages,
            "servers": combined.server_ids,
            "created_files": combined.created_files,
            "skipped": combined.skipped,
        },
        "created_files": combined.created_files,
    }


def workspace_languages(pack: TaskContextPack, workspace: dict[str, Any]) -> list[str]:
    explicit_languages: list[str] = []
    inferred_languages: list[str] = []

    def add_language(value: Any, target: list[str]) -> None:
        for token in _language_tokens(value):
            normalized = LANGUAGE_ALIASES.get(token) or _safe_language_id(token)
            if normalized and normalized not in target:
                target.append(normalized)

    def add_metadata(data: Any) -> None:
        if not isinstance(data, dict):
            return
        for key in (
            "languages",
            "language",
            "implementation_languages",
            "source_languages",
            "primary_language",
            "programming_language",
            "lsp_languages",
        ):
            if key in data:
                add_language(data.get(key), explicit_languages)

    def add_paths(value: Any) -> None:
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, (list, tuple, set)):
            candidates = [str(item) for item in value if str(item).strip()]
        else:
            candidates = []
        for item in candidates:
            language = LANGUAGE_EXTENSIONS.get(Path(item).suffix.lower())
            if language and language not in inferred_languages:
                inferred_languages.append(language)

    metadata = dict(pack.metadata or {})
    add_metadata(workspace)
    add_metadata(metadata)
    add_metadata(pack.continuity)
    current = dict((pack.continuity or {}).get("current_milestone") or {})
    add_metadata(current)
    add_metadata(current.get("metadata"))

    plan_artifact = metadata.get("plan_artifact")
    if isinstance(plan_artifact, dict):
        add_metadata(plan_artifact.get("metadata"))
        for module in list(plan_artifact.get("modules") or []):
            if not isinstance(module, dict):
                continue
            add_metadata(module.get("metadata"))
            add_paths(module.get("owned_area") or module.get("owned_paths"))
            for milestone in list(module.get("internal_milestones") or module.get("milestones") or []):
                if isinstance(milestone, dict):
                    add_metadata(milestone.get("metadata"))

    coder_work_order = metadata.get("coder_work_order")
    if isinstance(coder_work_order, dict):
        add_metadata(coder_work_order.get("metadata"))
        add_paths(coder_work_order.get("owned_area"))
        milestone = coder_work_order.get("current_milestone")
        if isinstance(milestone, dict):
            add_metadata(milestone.get("metadata"))

    prompt_view = metadata.get("prompt_view")
    if isinstance(prompt_view, dict):
        for key in ("module", "milestone", "workspace"):
            value = prompt_view.get(key)
            if isinstance(value, dict):
                add_metadata(value)
                add_metadata(value.get("metadata"))
        module = prompt_view.get("module")
        if isinstance(module, dict):
            add_paths(module.get("owned_area") or module.get("owned_paths"))

    return explicit_languages or inferred_languages


def _language_tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        collected: list[str] = []
        for key in ("language", "name", "id", "value"):
            collected.extend(_language_tokens(value.get(key)))
        return collected
    if isinstance(value, (list, tuple, set)):
        collected: list[str] = []
        for item in value:
            collected.extend(_language_tokens(item))
        return collected
    text = str(value or "").strip().lower()
    if not text:
        return []
    for separator in ("\n", "\t", ",", ";", "/", "|"):
        text = text.replace(separator, ",")
    text = text.replace(" and ", ",").replace(" & ", ",")
    return [part.strip().replace("_", "-") for part in text.split(",") if part.strip()]


def _safe_language_id(value: str) -> str:
    return "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum() or ch in {"-", "+", "#", "."}).strip()


def _available_lsp_configs(runtime_root: Path | None = None) -> dict[str, Any]:
    discovered: dict[str, Any] = {}
    try:
        for template in load_builtin_lsp_templates():
            discovered[template.config.server_id] = template
    except Exception:
        return {}
    if runtime_root is not None:
        root = lsp_config_root(Path(runtime_root))
        for path in sorted((*root.glob("*.toml"), *root.glob("*.json"))):
            try:
                for config in load_lsp_server_file(path):
                    discovered[config.config.server_id] = config
            except Exception:
                continue
    return {server_id: config for server_id, config in discovered.items() if config.enabled}


def _add_template_backed_lsp_servers(
    patch: WorkspaceEnvironmentPatch,
    *,
    languages: list[str],
    available_lsp_configs: dict[str, Any],
) -> None:
    if not languages:
        return
    by_language: dict[str, list[str]] = {}
    for server_id, file_config in sorted(available_lsp_configs.items()):
        config = getattr(file_config, "config", None)
        for language_id in getattr(config, "language_ids", ()) or ():
            normalized = LANGUAGE_ALIASES.get(str(language_id).strip().lower()) or _safe_language_id(str(language_id))
            if normalized:
                by_language.setdefault(normalized, []).append(server_id)
    for language in languages:
        server_ids = by_language.get(language) or []
        if not server_ids:
            patch.skipped.append(f"{language} skipped; no LSP server template advertises language_id={language}")
            continue
        for server_id in server_ids:
            if server_id not in patch.server_ids:
                patch.server_ids.append(server_id)


def _default_clangd_config(languages: tuple[str, ...]) -> str:
    if "cpp" in languages or "objcpp" in languages:
        standard = "c++20"
    elif "c" in languages or "objc" in languages:
        standard = "c17"
    else:
        standard = "c++20"
    return "\n".join(
        [
            "# Generated by Pal minion workspace setup for clangd.",
            "CompileFlags:",
            f"  Add: [-std={standard}]",
            "",
        ]
    )

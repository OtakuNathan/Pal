from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class LanguageLspEnvironmentSpec:
    language: str
    server_id: str
    command: str
    project_markers: tuple[str, ...] = ()
    context_strategy: str = "workspace_default"
    supports_inferred_project: bool = True


_LANGUAGE_ALIASES = {
    "c++": "cpp",
    "cxx": "cpp",
    "cs": "csharp",
    "js": "javascript",
    "jsx": "javascript",
    "py": "python",
    "rs": "rust",
    "sh": "shell",
    "shellscript": "shell",
    "ts": "typescript",
    "tsx": "typescript",
}

_SPECS = {
    "c": LanguageLspEnvironmentSpec("c", "clangd", "clangd", context_strategy="compile_database"),
    "cpp": LanguageLspEnvironmentSpec("cpp", "clangd", "clangd", context_strategy="compile_database"),
    "objective-c": LanguageLspEnvironmentSpec(
        "objective-c", "clangd", "clangd", context_strategy="compile_database"
    ),
    "objective-cpp": LanguageLspEnvironmentSpec(
        "objective-cpp", "clangd", "clangd", context_strategy="compile_database"
    ),
    "python": LanguageLspEnvironmentSpec(
        "python",
        "pyright",
        "pyright-langserver",
        ("pyrightconfig.json", "pyproject.toml", "setup.cfg", "setup.py"),
    ),
    "javascript": LanguageLspEnvironmentSpec(
        "javascript",
        "typescript",
        "typescript-language-server",
        ("jsconfig.json", "tsconfig.json", "package.json"),
    ),
    "typescript": LanguageLspEnvironmentSpec(
        "typescript",
        "typescript",
        "typescript-language-server",
        ("tsconfig.json", "jsconfig.json", "package.json"),
    ),
    "go": LanguageLspEnvironmentSpec("go", "gopls", "gopls", ("go.work", "go.mod")),
    "rust": LanguageLspEnvironmentSpec(
        "rust",
        "rust_analyzer",
        "rust-analyzer",
        ("rust-project.json", "Cargo.toml"),
        supports_inferred_project=False,
    ),
    "java": LanguageLspEnvironmentSpec(
        "java",
        "jdtls",
        "jdtls",
        ("pom.xml", "build.gradle", "build.gradle.kts", ".project"),
        supports_inferred_project=False,
    ),
    "csharp": LanguageLspEnvironmentSpec(
        "csharp",
        "csharp",
        "csharp-ls",
        ("*.sln", "*.csproj"),
        supports_inferred_project=False,
    ),
    "lua": LanguageLspEnvironmentSpec("lua", "lua", "lua-language-server", (".luarc.json",)),
    "shell": LanguageLspEnvironmentSpec("shell", "bash", "bash-language-server"),
    "html": LanguageLspEnvironmentSpec("html", "html", "vscode-html-language-server"),
    "css": LanguageLspEnvironmentSpec("css", "css", "vscode-css-language-server"),
    "json": LanguageLspEnvironmentSpec("json", "json", "vscode-json-language-server"),
    "yaml": LanguageLspEnvironmentSpec("yaml", "yaml", "yaml-language-server"),
}

_LANGUAGE_BY_SUFFIX = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".cs": "csharp",
    ".css": "css",
    ".h": "cpp",
    ".hh": "cpp",
    ".htm": "html",
    ".html": "html",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".go": "go",
    ".java": "java",
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".jsonc": "json",
    ".less": "css",
    ".lua": "lua",
    ".m": "objective-c",
    ".mm": "objective-cpp",
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".scss": "css",
    ".sh": "shell",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".yaml": "yaml",
    ".yml": "yaml",
}

_IGNORED_DISCOVERY_DIRS = frozenset(
    {".git", ".hg", ".svn", ".venv", "node_modules", "build", "dist", "__pycache__"}
)


def normalize_lsp_language(value: str) -> str:
    normalized = str(value or "").strip().lower()
    return _LANGUAGE_ALIASES.get(normalized, normalized)


def detect_workspace_languages(
    workspace_root: Path | None,
    *,
    limit: int = 5000,
) -> tuple[list[str], int]:
    if workspace_root is None or not workspace_root.is_dir():
        return [], 0
    counts: dict[str, int] = {}
    scanned = 0
    for path in workspace_root.rglob("*"):
        if any(part in _IGNORED_DISCOVERY_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        scanned += 1
        language = _LANGUAGE_BY_SUFFIX.get(path.suffix.lower())
        if language:
            counts[language] = counts.get(language, 0) + 1
        if scanned >= limit:
            break
    return sorted(counts, key=lambda item: (-counts[item], item)), scanned


def prepare_workspace_lsp_environment(
    *,
    workspace_root: Path | None,
    primary_language: str,
    context_root: Path | None,
    workspace: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    if workspace_root is not None:
        workspace_root = workspace_root.expanduser().resolve()
    if context_root is not None:
        context_root = context_root.expanduser().resolve()
    primary = normalize_lsp_language(primary_language)
    requested_languages = _dedupe(
        [
            primary,
            *[
                normalize_lsp_language(value)
                for value in list(
                    workspace.get("lsp_secondary_languages")
                    or workspace.get("workspace_languages")
                    or workspace.get("languages")
                    or []
                )
            ],
        ]
    )
    environments: dict[str, dict[str, Any]] = {}
    unavailable: list[dict[str, str]] = []
    available_servers: list[str] = []

    for language in requested_languages:
        spec = _SPECS.get(language)
        if spec is None:
            unavailable.append(
                {
                    "language": language,
                    "server_id": "",
                    "reason": "unsupported_primary_language" if language == primary else "unsupported_language",
                }
            )
            continue
        context = _prepare_project_context(
            spec,
            workspace_root=workspace_root,
            context_root=context_root,
            workspace=workspace,
        )
        environment = environments.setdefault(
            spec.server_id,
            {
                "server_id": spec.server_id,
                "languages": [],
                "project_context": context,
            },
        )
        environment["languages"] = _dedupe([*list(environment["languages"]), language])
        if str(context.get("status") or "") == "ready":
            if spec.server_id not in available_servers:
                available_servers.append(spec.server_id)
        else:
            unavailable.append(
                {
                    "language": language,
                    "server_id": spec.server_id,
                    "reason": str(context.get("reason") or "project_context_unavailable"),
                }
            )

    primary_spec = _SPECS.get(primary)
    primary_server = primary_spec.server_id if primary_spec is not None else ""
    primary_environment = dict(environments.get(primary_server) or {})
    primary_context = dict(primary_environment.get("project_context") or {})
    setup = {
        "primary_language": primary,
        "primary_server": primary_server,
        "servers": available_servers,
        "languages": requested_languages,
        "environments": environments,
        "project_contexts": {
            server_id: dict(environment.get("project_context") or {})
            for server_id, environment in environments.items()
        },
        "require_project_context": True,
        "status": (
            "ready"
            if primary_server in available_servers and str(primary_context.get("status") or "") == "ready"
            else "unavailable"
        ),
    }
    return setup, unavailable


def _prepare_project_context(
    spec: LanguageLspEnvironmentSpec,
    *,
    workspace_root: Path | None,
    context_root: Path | None,
    workspace: Mapping[str, Any],
) -> dict[str, Any]:
    if workspace_root is None or not workspace_root.is_dir():
        return {"status": "unavailable", "reason": "missing_workspace_root"}
    if spec.context_strategy == "compile_database":
        return _prepare_compile_database_context(
            workspace_root,
            context_root=context_root,
            workspace=workspace,
        )
    marker = _first_project_marker(workspace_root, spec.project_markers)
    if marker is not None:
        return {
            "status": "ready",
            "kind": "project_model",
            "source_path": str(marker),
            "workspace_root": str(workspace_root),
            "manager_generated": False,
            "fidelity": "project",
            "session_args": [],
        }
    if spec.supports_inferred_project:
        return {
            "status": "ready",
            "kind": "workspace_default",
            "workspace_root": str(workspace_root),
            "manager_generated": False,
            "fidelity": "fallback",
            "session_args": [],
        }
    return {
        "status": "unavailable",
        "reason": "missing_project_model",
        "workspace_root": str(workspace_root),
    }


def _prepare_compile_database_context(
    workspace_root: Path,
    *,
    context_root: Path | None,
    workspace: Mapping[str, Any],
) -> dict[str, Any]:
    explicit = str(
        workspace.get("compile_commands_path")
        or ""
    ).strip()
    compile_commands = (
        Path(explicit).expanduser().resolve()
        if explicit
        else _find_compile_commands(workspace_root)
    )
    if compile_commands is not None and compile_commands.is_file():
        return _compile_context(
            kind="compile_commands",
            source_path=compile_commands,
            workspace_root=workspace_root,
            manager_generated=False,
            fidelity="project",
        )

    compile_flags = workspace_root / "compile_flags.txt"
    if compile_flags.is_file():
        return _compile_context(
            kind="compile_flags",
            source_path=compile_flags,
            workspace_root=workspace_root,
            manager_generated=False,
            fidelity="project",
        )

    clangd_config = workspace_root / ".clangd"
    if clangd_config.is_file():
        return {
            "status": "ready",
            "kind": "clangd_config",
            "source_path": str(clangd_config),
            "workspace_root": str(workspace_root),
            "manager_generated": False,
            "fidelity": "project",
            "session_args": [],
        }

    if context_root is None:
        return {
            "status": "unavailable",
            "reason": "missing_compile_context",
            "workspace_root": str(workspace_root),
        }

    flags = _fallback_compile_flags(
        workspace_root,
        workspace=workspace,
    )
    descriptor = {
        "schema_version": "1",
        "workspace_root": str(workspace_root),
        "flags": flags,
    }
    context_key = hashlib.sha256(
        json.dumps(descriptor, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    context_dir = Path(context_root) / context_key
    compile_flags_path = context_dir / "compile_flags.txt"
    _atomic_write_text(
        compile_flags_path,
        "".join(f"{flag}\n" for flag in flags),
    )
    return _compile_context(
        kind="generated_compile_flags",
        source_path=compile_flags_path,
        workspace_root=workspace_root,
        manager_generated=True,
        fidelity="fallback",
    ) | {"flags": flags}


def _compile_context(
    *,
    kind: str,
    source_path: Path,
    workspace_root: Path,
    manager_generated: bool,
    fidelity: str,
) -> dict[str, Any]:
    compile_commands_dir = source_path.parent
    return {
        "status": "ready",
        "kind": kind,
        "source_path": str(source_path),
        "workspace_root": str(workspace_root),
        "compile_commands_dir": str(compile_commands_dir),
        "manager_generated": manager_generated,
        "fidelity": fidelity,
        "session_args": [f"--compile-commands-dir={compile_commands_dir}"],
    }


def _find_compile_commands(workspace_root: Path) -> Path | None:
    candidates = [
        workspace_root / "compile_commands.json",
        workspace_root / "build" / "compile_commands.json",
        workspace_root / "out" / "compile_commands.json",
        *sorted(workspace_root.glob("cmake-build-*/compile_commands.json")),
    ]
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)


def _first_project_marker(workspace_root: Path, markers: Sequence[str]) -> Path | None:
    for marker in markers:
        matches = (
            sorted(workspace_root.glob(marker))
            if any(token in marker for token in "*?[")
            else [workspace_root / marker]
        )
        found = next((candidate.resolve() for candidate in matches if candidate.exists()), None)
        if found is not None:
            return found
    return None


def _fallback_compile_flags(
    workspace_root: Path,
    *,
    workspace: Mapping[str, Any],
) -> list[str]:
    configured = [
        str(value).strip()
        for value in list(
            workspace.get("lsp_compile_flags")
            or []
        )
        if str(value).strip() and "\n" not in str(value) and "\r" not in str(value)
    ]
    # Keep fallback contexts useful without asking the model to manufacture a
    # compile database.  The workspace root alone does not resolve the common
    # project form `#include "package/header.hpp"` when public headers live
    # under include/.  These roots are discovered, never created, and the
    # generated flags stay in the manager-owned LSP context directory.
    include_roots = [workspace_root]
    for relative in ("include", "inc"):
        candidate = (workspace_root / relative).resolve()
        if candidate.is_dir() and candidate not in include_roots:
            include_roots.append(candidate)
    for value in [
        *list(workspace.get("include_paths") or []),
        *list(workspace.get("stub_include_paths") or []),
    ]:
        text = str(value).strip()
        if not text:
            continue
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = workspace_root / candidate
        candidate = candidate.resolve()
        if candidate.is_dir() and candidate not in include_roots:
            include_roots.append(candidate)
    standard = str(workspace.get("cpp_standard") or "").strip()
    standard_flags = (
        [f"-std={standard}"]
        if standard and not any(flag.startswith("-std=") for flag in configured)
        else []
    )
    return _dedupe([*configured, *standard_flags, *[f"-I{path}" for path in include_roots]])


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _dedupe(values: Sequence[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result

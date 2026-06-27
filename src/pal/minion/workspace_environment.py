from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from importlib import resources
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


@dataclass(frozen=True)
class WorkspaceEnvironmentTemplate:
    preparer_id: str
    kind: str
    language_ids: tuple[str, ...]
    required_lsp_server_ids: tuple[str, ...] = ()
    server_ids: tuple[str, ...] = ()
    repo_markers: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    readonly_skip: str = ""
    env_vars: dict[str, str] = field(default_factory=dict)
    path_prepend: tuple[dict[str, Any], ...] = ()
    files: tuple[dict[str, Any], ...] = ()
    source: str = ""


@dataclass
class WorkspaceEnvironmentPatch:
    server_ids: list[str] = field(default_factory=list)
    created_files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    env_path_prepend: dict[str, list[str]] = field(default_factory=dict)
    env_vars: dict[str, str] = field(default_factory=dict)

    def merge(self, other: "WorkspaceEnvironmentPatch") -> None:
        for server_id in other.server_ids:
            if server_id not in self.server_ids:
                self.server_ids.append(server_id)
        for path in other.created_files:
            if path not in self.created_files:
                self.created_files.append(path)
        self.skipped.extend(other.skipped)
        for name, paths in other.env_path_prepend.items():
            target = self.env_path_prepend.setdefault(name, [])
            for path in paths:
                if path not in target:
                    target.append(path)
        for name, value in other.env_vars.items():
            if name and value:
                self.env_vars[name] = value

    def prepend_env_path(self, name: str, path: Path) -> None:
        resolved = str(Path(path))
        if not resolved:
            return
        target = self.env_path_prepend.setdefault(name, [])
        if resolved not in target:
            target.append(resolved)

    def set_env(self, name: str, value: str) -> None:
        key = str(name or "").strip()
        text = str(value or "").strip()
        if key and text:
            self.env_vars[key] = text


class WorkspaceEnvironmentPreparer(Protocol):
    preparer_id: str
    language_ids: tuple[str, ...]
    required_lsp_server_ids: tuple[str, ...]

    def prepare(self, context: WorkspaceEnvironmentContext) -> WorkspaceEnvironmentPatch:
        ...


class WorkspaceRuntimeEnvironmentPreparer(Protocol):
    preparer_id: str
    language_ids: tuple[str, ...]

    def prepare_runtime(self, context: WorkspaceEnvironmentContext) -> WorkspaceEnvironmentPatch:
        ...


@dataclass(frozen=True)
class TemplateRuntimeEnvironmentPreparer:
    template: WorkspaceEnvironmentTemplate

    @property
    def preparer_id(self) -> str:
        return self.template.preparer_id

    @property
    def language_ids(self) -> tuple[str, ...]:
        return self.template.language_ids

    def prepare_runtime(self, context: WorkspaceEnvironmentContext) -> WorkspaceEnvironmentPatch:
        patch = WorkspaceEnvironmentPatch()
        if self.template.repo_markers and not _any_relative_path_exists(context.repo_path, self.template.repo_markers):
            return patch
        for name, value in self.template.env_vars.items():
            patch.set_env(name, _expand_template_value(value, context))
        for item in self.template.path_prepend:
            if not _template_condition_matches(item, context):
                continue
            name = str(item.get("name") or "").strip()
            path_value = str(item.get("path") or "").strip()
            if not name or not path_value:
                continue
            patch.prepend_env_path(name, _template_path(context.repo_path, _expand_template_value(path_value, context)))
        return patch


@dataclass(frozen=True)
class TemplateWorkspaceEnvironmentPreparer:
    template: WorkspaceEnvironmentTemplate

    @property
    def preparer_id(self) -> str:
        return self.template.preparer_id

    @property
    def language_ids(self) -> tuple[str, ...]:
        return self.template.language_ids

    @property
    def required_lsp_server_ids(self) -> tuple[str, ...]:
        return self.template.required_lsp_server_ids

    def prepare(self, context: WorkspaceEnvironmentContext) -> WorkspaceEnvironmentPatch:
        patch = WorkspaceEnvironmentPatch(
            server_ids=list(self.template.server_ids),
            skipped=list(self.template.skipped),
        )
        if self.template.repo_markers and not _any_relative_path_exists(context.repo_path, self.template.repo_markers):
            return patch
        if not self.template.files:
            return patch
        if not context.write_files:
            patch.skipped.append(self.template.readonly_skip or "repo already existed; did not create baseline workspace config")
            return patch
        for item in self.template.files:
            if not _template_condition_matches(item, context):
                continue
            relative_path = str(item.get("path") or "").strip()
            if not relative_path:
                continue
            skip = _first_existing_relative_path(context.repo_path, _string_tuple(item.get("skip_if_exists")))
            if skip:
                patch.skipped.append(f"{skip} already exists")
                continue
            target = _template_path(context.repo_path, relative_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(_expand_template_value(str(item.get("content") or ""), context), encoding="utf-8")
            created = str(Path(relative_path))
            if created not in patch.created_files:
                patch.created_files.append(created)
        return patch


def workspace_environment_preparers(
    runtime_root: Path | None = None,
) -> tuple[tuple[WorkspaceEnvironmentPreparer, ...], tuple[WorkspaceRuntimeEnvironmentPreparer, ...]]:
    templates = _load_workspace_environment_templates(runtime_root=runtime_root)
    lsp_preparers: list[WorkspaceEnvironmentPreparer] = []
    runtime_preparers: list[WorkspaceRuntimeEnvironmentPreparer] = []
    for template in templates:
        if template.kind == "runtime":
            runtime_preparers.append(TemplateRuntimeEnvironmentPreparer(template))
        elif template.kind in {"lsp", "workspace"}:
            lsp_preparers.append(TemplateWorkspaceEnvironmentPreparer(template))
    return tuple(lsp_preparers), tuple(runtime_preparers)


PREPARERS: tuple[WorkspaceEnvironmentPreparer, ...]
RUNTIME_PREPARERS: tuple[WorkspaceRuntimeEnvironmentPreparer, ...]


def prepare_workspace_environment(
    repo_path: Path,
    pack: TaskContextPack,
    workspace: dict[str, Any],
    *,
    write_files: bool,
    runtime_root: Path | None = None,
    policy: dict[str, Any] | None = None,
    preparers: tuple[WorkspaceEnvironmentPreparer, ...] | None = None,
    runtime_preparers: tuple[WorkspaceRuntimeEnvironmentPreparer, ...] | None = None,
    available_lsp_server_ids: set[str] | None = None,
) -> dict[str, Any]:
    languages = workspace_languages(pack, workspace, repo_path=repo_path)
    if not languages:
        return {}
    if preparers is None:
        preparers = PREPARERS
    if runtime_preparers is None:
        runtime_preparers = RUNTIME_PREPARERS
    if preparers is PREPARERS and runtime_preparers is RUNTIME_PREPARERS:
        preparers, runtime_preparers = workspace_environment_preparers(runtime_root=runtime_root)
    environment_policy = _workspace_environment_policy(pack, workspace, policy)
    runtime_enabled = _policy_bool(environment_policy, "runtime", default=True)
    lsp_enabled = _policy_bool(environment_policy, "lsp", default=True)
    baseline_write_enabled = _policy_bool(environment_policy, "write_baseline_config", default=True)
    allowed_preparers = set(_policy_preparers(environment_policy))
    available_lsp_configs = _available_lsp_configs(runtime_root) if lsp_enabled and available_lsp_server_ids is None else {}
    available_servers = frozenset(
        available_lsp_server_ids if available_lsp_server_ids is not None else set(available_lsp_configs)
    )
    context = WorkspaceEnvironmentContext(
        repo_path=Path(repo_path),
        languages=tuple(languages),
        write_files=bool(write_files and baseline_write_enabled),
        available_lsp_server_ids=available_servers,
    )
    combined = WorkspaceEnvironmentPatch()
    if runtime_enabled:
        for preparer in runtime_preparers:
            if not _preparer_allowed(preparer, languages=languages, allowed_preparers=allowed_preparers):
                continue
            combined.merge(preparer.prepare_runtime(context))
    handled_languages: set[str] = set()
    if lsp_enabled:
        for preparer in preparers:
            matched_languages = set(preparer.language_ids).intersection(languages)
            if not matched_languages:
                continue
            if not _preparer_allowed(preparer, languages=languages, allowed_preparers=allowed_preparers):
                continue
            handled_languages.update(matched_languages)
            missing = [server_id for server_id in preparer.required_lsp_server_ids if server_id not in available_servers]
            if missing:
                combined.skipped.append(
                    f"{'/'.join(preparer.language_ids)} skipped; missing LSP server template(s): {', '.join(missing)}"
                )
                continue
            combined.merge(preparer.prepare(context))
    if available_lsp_configs and lsp_enabled:
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
        "execution_env": _execution_env_payload(combined),
        "created_files": combined.created_files,
    }


def _workspace_environment_policy(
    pack: TaskContextPack,
    workspace: dict[str, Any],
    explicit: dict[str, Any] | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    profile = dict(pack.resolved_profile or {})
    for candidate in (
        profile.get("workspace_environment_policy"),
        profile.get("effective_workspace_environment_policy"),
        workspace.get("workspace_environment_policy"),
        workspace.get("workspace_environment"),
        explicit,
    ):
        if isinstance(candidate, dict):
            result.update(dict(candidate))
    return result


def _policy_bool(policy: dict[str, Any], key: str, *, default: bool) -> bool:
    value = policy.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled", "none"}
    return bool(value)


def _policy_preparers(policy: dict[str, Any]) -> list[str]:
    values = policy.get("preparers")
    if isinstance(values, str):
        raw = [values]
    elif isinstance(values, (list, tuple, set)):
        raw = list(values)
    else:
        raw = []
    result: list[str] = []
    for item in raw:
        for token in _language_tokens(item):
            normalized = LANGUAGE_ALIASES.get(token) or _safe_language_id(token)
            if normalized and normalized not in result and normalized not in {"all", "*"}:
                result.append(normalized)
    return result


def _preparer_allowed(preparer: Any, *, languages: list[str], allowed_preparers: set[str]) -> bool:
    language_ids = tuple(str(item) for item in tuple(getattr(preparer, "language_ids", ()) or ()))
    matched_languages = set(language_ids).intersection(languages)
    if not matched_languages:
        return False
    if not allowed_preparers:
        return True
    preparer_tokens = set(language_ids)
    preparer_id = _safe_language_id(str(getattr(preparer, "preparer_id", "") or "").replace("_", "-"))
    if preparer_id:
        preparer_tokens.add(preparer_id)
    return bool(preparer_tokens.intersection(allowed_preparers))


def _load_workspace_environment_templates(runtime_root: Path | None = None) -> tuple[WorkspaceEnvironmentTemplate, ...]:
    templates: dict[str, WorkspaceEnvironmentTemplate] = {}
    for template in _load_builtin_workspace_environment_templates():
        templates[template.preparer_id] = template
    if runtime_root is not None:
        root = _runtime_workspace_environment_root(Path(runtime_root))
        for path in sorted((*root.rglob("*.toml"), *root.rglob("*.json"))):
            try:
                for template in _load_workspace_environment_template_file(path, source="runtime"):
                    templates[template.preparer_id] = template
            except Exception:
                continue
    return tuple(templates[key] for key in sorted(templates))


def _load_builtin_workspace_environment_templates() -> tuple[WorkspaceEnvironmentTemplate, ...]:
    try:
        root = resources.files("pal.minion").joinpath("workspace_environment_templates")
        items = sorted(root.iterdir(), key=lambda entry: entry.name)
    except Exception:
        return ()
    templates: list[WorkspaceEnvironmentTemplate] = []
    for item in items:
        if item.name.startswith("_") or not item.name.endswith((".toml", ".json")):
            continue
        try:
            templates.extend(
                _load_workspace_environment_template_payload(
                    tomllib.loads(item.read_text(encoding="utf-8")),
                    source="builtin_template",
                )
            )
        except Exception:
            continue
    return tuple(templates)


def _runtime_workspace_environment_root(runtime_root: Path) -> Path:
    return Path(runtime_root) / "plugins" / "minion" / "workspace_environment"


def _load_workspace_environment_template_file(path: Path, *, source: str) -> tuple[WorkspaceEnvironmentTemplate, ...]:
    if path.suffix.lower() == ".json":
        import json

        payload = dict(json.loads(path.read_text(encoding="utf-8")))
    else:
        payload = dict(tomllib.loads(path.read_text(encoding="utf-8")))
    return _load_workspace_environment_template_payload(payload, source=source)


def _load_workspace_environment_template_payload(payload: dict[str, Any], *, source: str) -> tuple[WorkspaceEnvironmentTemplate, ...]:
    data = dict(payload or {})
    raw_preparers = data.get("preparers")
    if isinstance(raw_preparers, list):
        return tuple(
            _template_from_payload(dict(item or {}), source=source)
            for item in raw_preparers
            if isinstance(item, dict)
        )
    if isinstance(raw_preparers, dict):
        return tuple(
            _template_from_payload({**dict(value or {}), "preparer_id": str(preparer_id)}, source=source)
            for preparer_id, value in raw_preparers.items()
            if isinstance(value, dict)
        )
    return (_template_from_payload(data, source=source),)


def _template_from_payload(payload: dict[str, Any], *, source: str) -> WorkspaceEnvironmentTemplate:
    data = dict(payload or {})
    preparer_id = str(data.get("preparer_id") or data.get("id") or "").strip()
    if not preparer_id:
        raise ValueError("workspace environment template lacks preparer_id")
    kind = str(data.get("kind") or data.get("type") or "lsp").strip().lower() or "lsp"
    env = dict(data.get("env") or {})
    return WorkspaceEnvironmentTemplate(
        preparer_id=preparer_id,
        kind=kind,
        language_ids=tuple(_normalize_language_ids(data.get("language_ids") or data.get("languages"))),
        required_lsp_server_ids=_string_tuple(data.get("required_lsp_server_ids") or data.get("required_servers")),
        server_ids=_string_tuple(data.get("server_ids") or data.get("servers")),
        repo_markers=_string_tuple(data.get("repo_markers") or data.get("workspace_markers")),
        skipped=_string_tuple(data.get("skipped")),
        readonly_skip=str(data.get("readonly_skip") or "").strip(),
        env_vars={str(key): str(value) for key, value in dict(env.get("vars") or data.get("env_vars") or {}).items()},
        path_prepend=tuple(
            dict(item or {})
            for item in list(env.get("path_prepend") or data.get("path_prepend") or [])
            if isinstance(item, dict)
        ),
        files=tuple(dict(item or {}) for item in list(data.get("files") or []) if isinstance(item, dict)),
        source=source,
    )


def _normalize_language_ids(value: Any) -> list[str]:
    result: list[str] = []
    for token in _language_tokens(value):
        normalized = LANGUAGE_ALIASES.get(token) or _safe_language_id(token)
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, (list, tuple, set)):
        return ()
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return tuple(result)


def _any_relative_path_exists(repo_path: Path, values: tuple[str, ...]) -> bool:
    return any(_template_path(repo_path, value).exists() for value in values if str(value or "").strip())


def _first_existing_relative_path(repo_path: Path, values: tuple[str, ...]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text and _template_path(repo_path, text).exists():
            return text
    return ""


def _template_condition_matches(item: dict[str, Any], context: WorkspaceEnvironmentContext) -> bool:
    if_exists = _string_tuple(item.get("if_exists"))
    if if_exists and not _any_relative_path_exists(context.repo_path, if_exists):
        return False
    if_any_exists = _string_tuple(item.get("if_any_exists"))
    if if_any_exists and not _any_relative_path_exists(context.repo_path, if_any_exists):
        return False
    unless_exists = _string_tuple(item.get("unless_exists"))
    if unless_exists and _any_relative_path_exists(context.repo_path, unless_exists):
        return False
    unless_any_exists = _string_tuple(item.get("unless_any_exists"))
    if unless_any_exists and _any_relative_path_exists(context.repo_path, unless_any_exists):
        return False
    repo_language = str(item.get("if_repo_language") or "").strip()
    if repo_language:
        normalized = LANGUAGE_ALIASES.get(repo_language.lower()) or _safe_language_id(repo_language)
        if normalized == "python" and not _looks_like_python_repo(context.repo_path):
            return False
        if normalized and normalized not in context.languages:
            return False
    language = str(item.get("if_language") or "").strip()
    if language:
        normalized = LANGUAGE_ALIASES.get(language.lower()) or _safe_language_id(language)
        if normalized and normalized not in context.languages:
            return False
    return True


def _template_path(repo_path: Path, value: str) -> Path:
    path = Path(str(value or ""))
    if path.is_absolute():
        return path
    return Path(repo_path) / path


def _expand_template_value(value: str, context: WorkspaceEnvironmentContext) -> str:
    jobs = max(1, min(int(os.cpu_count() or 2), 8))
    replacements = {
        "repo_path": str(context.repo_path),
        "cpu_count": str(int(os.cpu_count() or 1)),
        "cpu_count_max_8": str(jobs),
        "clangd_standard": _clangd_standard(context.languages),
    }
    result = str(value or "")
    for key, replacement in replacements.items():
        result = result.replace(f"${{{key}}}", replacement).replace(f"{{{key}}}", replacement)
    return result


def _execution_env_payload(patch: WorkspaceEnvironmentPatch) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if patch.env_vars:
        result["vars"] = {
            key: value
            for key, value in sorted(patch.env_vars.items())
            if key and value
        }
    path_prepend = {
        key: list(paths)
        for key, paths in sorted(patch.env_path_prepend.items())
        if key and paths
    }
    if path_prepend:
        result["path_prepend"] = path_prepend
    return result


def workspace_languages(pack: TaskContextPack, workspace: dict[str, Any], *, repo_path: Path | None = None) -> list[str]:
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
        current_module_id = str(metadata.get("module_id") or "").strip()
        for module in list(plan_artifact.get("modules") or []):
            if not isinstance(module, dict):
                continue
            if current_module_id and str(module.get("module_id") or module.get("id") or "").strip() != current_module_id:
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

    repo_languages = _repo_languages(repo_path)
    return _ordered_unique([*explicit_languages, *repo_languages, *inferred_languages])


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


def _repo_languages(repo_path: Path | None) -> list[str]:
    if repo_path is None:
        return []
    repo = Path(repo_path)
    if not repo.exists() or not repo.is_dir():
        return []
    manifest_languages = _repo_manifest_languages(repo)
    source_languages = _repo_source_languages(repo)
    return _ordered_unique([*manifest_languages, *source_languages])


def _repo_manifest_languages(repo_path: Path) -> list[str]:
    manifests = {
        "pyproject.toml": "python",
        "setup.py": "python",
        "setup.cfg": "python",
        "requirements.txt": "python",
        "Pipfile": "python",
        "poetry.lock": "python",
        "pytest.ini": "python",
        "tox.ini": "python",
        "CMakeLists.txt": "cpp",
        "package.json": "javascript",
        "tsconfig.json": "typescript",
        "go.mod": "go",
        "Cargo.toml": "rust",
    }
    languages: list[str] = []
    for file_name, language in manifests.items():
        if (repo_path / file_name).exists() and language not in languages:
            languages.append(language)
    return languages


def _repo_source_languages(repo_path: Path, *, max_files: int = 500) -> list[str]:
    counts: dict[str, int] = {}
    inspected = 0
    for path in _iter_repo_source_paths(repo_path):
        language = LANGUAGE_EXTENSIONS.get(path.suffix.lower())
        if not language:
            continue
        counts[language] = counts.get(language, 0) + 1
        inspected += 1
        if inspected >= max_files:
            break
    return [
        language
        for language, _count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def _iter_repo_source_paths(repo_path: Path):
    ignored_dirs = {
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
    }
    try:
        walker = os.walk(repo_path)
        for root, dirnames, filenames in walker:
            dirnames[:] = [name for name in dirnames if name not in ignored_dirs]
            current = Path(root)
            for file_name in filenames:
                yield current / file_name
    except Exception:
        return


def _looks_like_python_repo(repo_path: Path) -> bool:
    repo = Path(repo_path)
    return "python" in _repo_manifest_languages(repo) or "python" in _repo_source_languages(repo, max_files=50)


def _ordered_unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


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
    standard = _clangd_standard(languages)
    return "\n".join(
        [
            "# Generated by Pal minion workspace setup for clangd.",
            "CompileFlags:",
            f"  Add: [-std={standard}]",
            "",
        ]
    )


def _clangd_standard(languages: tuple[str, ...]) -> str:
    if "cpp" in languages or "objcpp" in languages:
        return "c++20"
    elif "c" in languages or "objc" in languages:
        return "c17"
    return "c++20"


PREPARERS, RUNTIME_PREPARERS = workspace_environment_preparers()

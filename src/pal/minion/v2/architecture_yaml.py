from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode
from yaml.tokens import AliasToken, AnchorToken, TagToken

from pal.minion.v2.module_protocol import (
    ModuleDefinition,
    SemanticName,
    SemanticText,
)


ARCHITECTURE_DRAFT_SCHEMA_VERSION = 4
ARCHITECTURE_DRAFT_FILENAME = "architecture.yaml"
_MAX_ARCHITECTURE_DRAFT_BYTES = 2 * 1024 * 1024
_SEMANTIC_NAME = SemanticName
_NONEMPTY_TEXT = SemanticText
class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ArchitecturePathScope(_StrictModel):
    kind: Literal["file", "directory"]
    path: str


class ArchitectureModulePaths(_StrictModel):
    contract_mode: Literal["file_frozen", "review_guarded"]
    contract_paths: list[str]
    implementation_scopes: list[ArchitecturePathScope]
    reference_only: list[str]


class ArchitectureModule(ModuleDefinition):
    paths: ArchitectureModulePaths


class ArchitectureRequirement(_StrictModel):
    claim: _NONEMPTY_TEXT
    owner: _SEMANTIC_NAME
    contract_path: list[_NONEMPTY_TEXT] = Field(
        min_length=1,
        description=(
            "Ordered public semantic interface/signal chain from owner to observable outcome; "
            "not a filesystem allowlist and not bare source filenames."
        ),
    )


class ArchitectureScenario(_StrictModel):
    modules: list[_SEMANTIC_NAME] = Field(min_length=1)
    requirement_refs: list[_SEMANTIC_NAME] = Field(min_length=1)
    entrypoint: _NONEMPTY_TEXT
    contract_flow: list[_NONEMPTY_TEXT] = Field(min_length=1)
    observable_behavior: _NONEMPTY_TEXT
    failure_behavior: _NONEMPTY_TEXT
    environment: _NONEMPTY_TEXT


class ArchitectureDraft(_StrictModel):
    schema_version: Literal[ARCHITECTURE_DRAFT_SCHEMA_VERSION]
    requirements: dict[_SEMANTIC_NAME, ArchitectureRequirement]
    modules: dict[_SEMANTIC_NAME, ArchitectureModule]
    scenarios: dict[_SEMANTIC_NAME, ArchitectureScenario]


class ArchitectureDraftFileError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        path: str = "architecture.yaml",
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.errors = list(errors or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "error_type": self.__class__.__name__,
            "code": self.code,
            "path": self.path,
            **({"errors": self.errors} if self.errors else {}),
        }


class _ArchitectureYamlLoader(yaml.SafeLoader):
    """Safe YAML 1.2-ish loader with aliases, tags, merges, and duplicate keys disabled."""

    yaml_implicit_resolvers = copy.deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise ConstructorError(None, None, "YAML aliases are not allowed", self.peek_event().start_mark)
        return super().compose_node(parent, index)

    def flatten_mapping(self, node: MappingNode) -> None:
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "YAML merge keys are not allowed",
                    key_node.start_mark,
                )
        super().flatten_mapping(node)

    def construct_mapping(self, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
        if not isinstance(node, MappingNode):
            return super().construct_mapping(node, deep=deep)
        self.flatten_mapping(node)
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in result
            except TypeError as exc:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "mapping keys must be scalar values",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key is not allowed: {key!r}",
                    key_node.start_mark,
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result


for _resolver_key, _resolvers in list(_ArchitectureYamlLoader.yaml_implicit_resolvers.items()):
    _ArchitectureYamlLoader.yaml_implicit_resolvers[_resolver_key] = [
        (tag, pattern)
        for tag, pattern in _resolvers
        if tag not in {"tag:yaml.org,2002:bool", "tag:yaml.org,2002:timestamp"}
    ]
_ArchitectureYamlLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", re.IGNORECASE),
    list("tTfF"),
)


def architecture_draft_path(workspace: Mapping[str, Any]) -> Path:
    explicit = str(workspace.get("architecture_draft_path") or "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()
    run_dir = str(workspace.get("run_dir") or "").strip()
    if run_dir:
        return (Path(run_dir).expanduser().resolve() / ARCHITECTURE_DRAFT_FILENAME)
    runtime_root = Path(str(workspace.get("runtime_root") or "")).expanduser().resolve()
    invocation_id = str(
        dict(workspace.get("minion_v2") or {}).get("invocation_id")
        or workspace.get("invocation_id")
        or "unbound"
    ).strip()
    return runtime_root / "data" / "minion" / "runtime" / "authoring" / invocation_id / ARCHITECTURE_DRAFT_FILENAME


def prepare_architecture_draft_file(workspace: dict[str, Any]) -> Path:
    path = architecture_draft_path(workspace)
    workspace["architecture_draft_path"] = str(path)
    if path.exists():
        if not path.is_file():
            raise ArchitectureDraftFileError(
                "architecture Draft path is not a file",
                code="draft_not_a_file",
                path=str(path),
            )
        return path
    base = workspace.get("architecture_revision_base_submission")
    submission = (
        dict(base)
        if isinstance(base, Mapping)
        else {"requirements": {}, "modules": {}, "scenarios": {}}
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_architecture_draft(submission), encoding="utf-8")
    return path


def write_architecture_draft(
    workspace: dict[str, Any],
    submission: Mapping[str, Any],
) -> Path:
    path = architecture_draft_path(workspace)
    workspace["architecture_draft_path"] = str(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_architecture_draft(submission), encoding="utf-8")
    return path


def render_architecture_draft(submission: Mapping[str, Any]) -> str:
    candidate = {
        "schema_version": ARCHITECTURE_DRAFT_SCHEMA_VERSION,
        "requirements": dict(submission.get("requirements") or {}),
        "modules": dict(submission.get("modules") or {}),
        "scenarios": dict(submission.get("scenarios") or {}),
    }
    try:
        normalized = ArchitectureDraft.model_validate(candidate, strict=True).model_dump(mode="python")
    except ValidationError as exc:
        raise _validation_error(exc) from exc
    header = (
        "# Pal Architect Draft. This is control-plane metadata, not product source.\n"
        "# Add or remove stable snake_case keys under requirements, modules, and scenarios.\n"
        "# architecture_submit validates this complete file and advances the workflow.\n"
        "# Requirement example: {claim: incomplete input fails observably, owner: example_cli, contract_path: [decoder::status -> command::EOF classification, command::exit -> process status]}\n"
        "# Module example: {module_kind: implementation, behavior_kind: service, responsibility: decode complete frames, dependencies: {}, contract: {inputs: {chunks: {interface: decoder::feed, semantics: arbitrary byte chunks}}, outputs: {frames: {interface: decoder::feed, semantics: complete payloads in wire order}}, errors: [oversized lengths fail deterministically], invariants: [partial payloads are never emitted]}, ownership: [each decoder owns its buffered input], lifecycle: {creation: starts empty, operation: accepts chunks, shutdown: caller stops feeding, failure: remains failed until reset, cleanup: destruction releases buffered input}, state_machine: null, paths: {contract_mode: file_frozen, contract_paths: [include/example.h], implementation_scopes: [{kind: file, path: src/example.cpp}], reference_only: []}}\n"
        "# Scenario example: {modules: [example_module], requirement_refs: [incomplete_input], entrypoint: example command, contract_flow: [example_module::frames -> command::stdout], observable_behavior: externally visible result, failure_behavior: exits nonzero with a diagnostic, environment: project host}\n"
    )
    return header + yaml.safe_dump(
        normalized,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )


def load_architecture_draft(workspace: Mapping[str, Any]) -> dict[str, Any]:
    path = architecture_draft_path(workspace)
    if not path.exists():
        raise ArchitectureDraftFileError(
            f"architecture Draft does not exist: {path}",
            code="draft_not_found",
            path=str(path),
        )
    if not path.is_file():
        raise ArchitectureDraftFileError(
            f"architecture Draft is not a file: {path}",
            code="draft_not_a_file",
            path=str(path),
        )
    size = path.stat().st_size
    if size > _MAX_ARCHITECTURE_DRAFT_BYTES:
        raise ArchitectureDraftFileError(
            f"architecture Draft exceeds {_MAX_ARCHITECTURE_DRAFT_BYTES} bytes",
            code="draft_too_large",
            path=str(path),
        )
    text = path.read_text(encoding="utf-8")
    try:
        for token in yaml.scan(text):
            if isinstance(token, (AliasToken, AnchorToken, TagToken)):
                raise ArchitectureDraftFileError(
                    "YAML anchors, aliases, and explicit tags are not allowed",
                    code="unsupported_yaml_feature",
                    path=str(path),
                )
        documents = list(yaml.load_all(text, Loader=_ArchitectureYamlLoader))
    except ArchitectureDraftFileError:
        raise
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ArchitectureDraftFileError(
            f"invalid architecture YAML: {exc}",
            code="invalid_yaml",
            path=str(path),
        ) from exc
    if len(documents) != 1:
        raise ArchitectureDraftFileError(
            "architecture Draft must contain exactly one YAML document",
            code="multiple_yaml_documents",
            path=str(path),
        )
    try:
        draft = ArchitectureDraft.model_validate(documents[0], strict=True)
    except ValidationError as exc:
        raise _validation_error(exc, path=str(path)) from exc
    value = draft.model_dump(mode="python")
    value.pop("schema_version", None)
    return value


def _validation_error(
    exc: ValidationError,
    *,
    path: str = ARCHITECTURE_DRAFT_FILENAME,
) -> ArchitectureDraftFileError:
    errors = [
        {
            "code": str(item.get("type") or "validation_error"),
            "path": ".".join(str(part) for part in tuple(item.get("loc") or ())),
            "message": str(item.get("msg") or "invalid value"),
            **({"value": item.get("input")} if _json_scalar(item.get("input")) else {}),
        }
        for item in exc.errors(include_url=False)
    ]
    summary = "; ".join(
        f"{item['path'] or 'architecture.yaml'}: {item['message']}" for item in errors[:8]
    )
    return ArchitectureDraftFileError(
        "architecture Draft schema validation failed" + (f": {summary}" if summary else ""),
        code="schema_validation_failed",
        path=path,
        errors=errors,
    )


def _json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))

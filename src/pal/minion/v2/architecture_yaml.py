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


ARCHITECTURE_DRAFT_SCHEMA_VERSION = 5
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


_ARCHITECTURE_DRAFT_SCHEMA_GUIDANCE = """
Pal Architect Draft. This is strict control-plane metadata, not product source.
Edit the live requirements/modules/scenarios maps below; do not uncomment or submit the example.

YAML and scalar rules:
- Exactly one YAML document. Anchors, aliases, explicit tags, merge keys, duplicate keys, and extra fields are forbidden.
- Every dynamic key (requirement, module, dependency, port, state, transition, scenario) is a stable snake_case semantic name matching ^[a-z][a-z0-9_]{1,79}$.
- Every semantic text value is a non-empty trimmed string of at most 4000 characters.
- Lists are YAML lists, maps are YAML maps, booleans are true/false, and null is null. Quote flow-style text containing commas or colons.

Top-level schema and closure:
- schema_version is the integer 5.
- requirements, modules, and scenarios are maps; each must be non-empty at submission.
- Names are unique across requirements, modules, and scenarios where they represent semantic graph nodes.
- Every requirement is consumed by at least one scenario. Its owner is exactly one declared module or scenario.
- Every module dependency names a declared provider, consumes declared provider output keys, and the module dependency graph is acyclic.
- A scenario lists implementation modules only. Include every transitive implementation dependency required by those selected modules, but omit unrelated modules and contract_only providers.
- Every scenario requirement_ref names a declared requirement. Use focused scenarios; a universal all-module scenario is invalid unless its real entrypoint requires that exact composition.

Requirement schema:
- claim: required semantic statement.
- owner: required module-or-scenario snake_case name.
- contract_path: non-empty ordered list of public interfaces/signals from owner to observable outcome; never use bare filenames.

Module schema:
- module_kind enum: implementation | contract_only.
- behavior_kind enum: stateless | resource_owner | service | workflow | adapter.
- responsibility: required single responsibility.
- dependencies: map, possibly empty. Each provider entry requires consumes (non-empty provider output-key list), purpose, and handoff.
- contract requires inputs, outputs, errors, and invariants. inputs may be empty; outputs must contain at least one port. Every port requires interface and semantics.
- ownership is a non-empty list.
- lifecycle requires creation, operation, shutdown, failure, and cleanup.
- state_machine is null when unnecessary. stateless modules must use null. A non-null state machine requires initial and a non-empty states map.
- Every state requires meaning and a transitions map. Each transition key is an event name and its value requires to and effect.
- The initial state and every transition target must exist; every declared state must be reachable from initial.

Path schema and policy:
- paths requires contract_mode, contract_paths, implementation_scopes, and reference_only.
- contract_mode enum: file_frozen | review_guarded. contract_only modules must use file_frozen.
- contract_paths is a non-empty list of repository-relative files owned by exactly one module.
- implementation_scopes is a list of {kind, path}; kind enum: file | directory. contract_only modules require an empty list.
- reference_only is a list of repository-relative read-only paths.
- Writable scopes, contract ownership, and reference-only paths must not conflict or overlap. Paths must stay below the repository root.
- Do not declare test_scopes. The Manager owns tests/<module_name>/developer and tests/<module_name>/verifier.
- `system` is reserved for the Manager-owned system verification corpus.

Scenario schema:
- modules: non-empty implementation-module list with the dependency-closure rule above.
- requirement_refs: non-empty requirement-name list.
- entrypoint: real product entrypoint.
- contract_flow: non-empty ordered list of interface/data/state/error handoffs.
- observable_behavior, failure_behavior, and environment are required semantic text.

BEGIN COMPLETE VALID EXAMPLE
"""

_ARCHITECTURE_DRAFT_SCHEMA_GUIDANCE_END = """
END COMPLETE VALID EXAMPLE
architecture_submit validates the complete live document and advances the workflow only after schema, semantic closure, path, Git-state, revision-scope, fencing, and snapshot-stability checks pass.
"""


def _architecture_draft_schema_example() -> dict[str, Any]:
    return {
        "schema_version": ARCHITECTURE_DRAFT_SCHEMA_VERSION,
        "requirements": {
            "decode_stream_completion": {
                "claim": "decode emits complete payloads and rejects incomplete EOF",
                "owner": "framepipe_cli",
                "contract_path": [
                    "frame_protocol::feed -> frames and status",
                    "framepipe_cli::decode -> stdout, stderr, and process exit",
                ],
            }
        },
        "modules": {
            "framing_contract": {
                "module_kind": "contract_only",
                "behavior_kind": "stateless",
                "responsibility": "define the shared framing bounds",
                "dependencies": {},
                "contract": {
                    "inputs": {},
                    "outputs": {
                        "max_payload_bytes": {
                            "interface": "framing_contract::max_payload_bytes",
                            "semantics": "maximum accepted payload size",
                        }
                    },
                    "errors": [],
                    "invariants": ["the bound is identical for encoder and decoder"],
                },
                "ownership": ["compile-time declaration owns no runtime state"],
                "lifecycle": {
                    "creation": "available when the contract is imported",
                    "operation": "consumers read the immutable bound",
                    "shutdown": "no runtime shutdown",
                    "failure": "no runtime failure",
                    "cleanup": "no runtime cleanup",
                },
                "state_machine": None,
                "paths": {
                    "contract_mode": "file_frozen",
                    "contract_paths": ["include/framepipe/framing.hpp"],
                    "implementation_scopes": [],
                    "reference_only": [],
                },
            },
            "frame_protocol": {
                "module_kind": "implementation",
                "behavior_kind": "resource_owner",
                "responsibility": "decode framed byte streams without partial emission",
                "dependencies": {
                    "framing_contract": {
                        "consumes": ["max_payload_bytes"],
                        "purpose": "enforce the shared payload bound",
                        "handoff": "read the immutable bound without ownership transfer",
                    }
                },
                "contract": {
                    "inputs": {
                        "chunks": {
                            "interface": "frame_protocol::feed",
                            "semantics": "arbitrary byte chunks in stream order",
                        }
                    },
                    "outputs": {
                        "frames": {
                            "interface": "frame_protocol::feed",
                            "semantics": "complete payloads in stream order",
                        },
                        "status": {
                            "interface": "frame_protocol::finish",
                            "semantics": "complete, incomplete, or failed terminal status",
                        },
                    },
                    "errors": ["oversized payload length enters failed state"],
                    "invariants": ["partial payloads are never emitted"],
                },
                "ownership": ["each decoder exclusively owns its buffered input"],
                "lifecycle": {
                    "creation": "starts in reading_header with an empty buffer",
                    "operation": "accepts chunks and emits complete frames",
                    "shutdown": "finish classifies any buffered input",
                    "failure": "failed rejects input until reset",
                    "cleanup": "destruction releases buffered input",
                },
                "state_machine": {
                    "initial": "reading_header",
                    "states": {
                        "reading_header": {
                            "meaning": "waiting for a complete length header",
                            "transitions": {
                                "header_ready": {
                                    "to": "reading_payload",
                                    "effect": "retain the decoded payload length",
                                },
                                "oversized_length": {
                                    "to": "failed",
                                    "effect": "discard buffered input and reject later input",
                                },
                            },
                        },
                        "reading_payload": {
                            "meaning": "waiting for the declared payload bytes",
                            "transitions": {
                                "frame_ready": {
                                    "to": "reading_header",
                                    "effect": "emit one complete frame",
                                },
                                "reset": {
                                    "to": "reading_header",
                                    "effect": "clear buffered input without emission",
                                },
                            },
                        },
                        "failed": {
                            "meaning": "rejecting input after a protocol error",
                            "transitions": {
                                "reset": {
                                    "to": "reading_header",
                                    "effect": "clear failure and buffered input",
                                }
                            },
                        },
                    },
                },
                "paths": {
                    "contract_mode": "review_guarded",
                    "contract_paths": ["include/framepipe/decoder.hpp"],
                    "implementation_scopes": [
                        {"kind": "file", "path": "src/decoder.cpp"}
                    ],
                    "reference_only": ["docs/framing_protocol.md"],
                },
            },
            "framepipe_cli": {
                "module_kind": "implementation",
                "behavior_kind": "workflow",
                "responsibility": "expose stream decoding through the command line",
                "dependencies": {
                    "frame_protocol": {
                        "consumes": ["frames", "status"],
                        "purpose": "decode standard input and classify EOF",
                        "handoff": "feed bytes, print frames, then inspect status",
                    }
                },
                "contract": {
                    "inputs": {
                        "stdin_bytes": {
                            "interface": "framepipe_cli::decode",
                            "semantics": "framed bytes from standard input",
                        }
                    },
                    "outputs": {
                        "process_result": {
                            "interface": "framepipe_cli::decode",
                            "semantics": "stdout frames, stderr diagnostic, and exit code",
                        }
                    },
                    "errors": ["incomplete EOF exits nonzero with a diagnostic"],
                    "invariants": ["only complete frames reach standard output"],
                },
                "ownership": ["one command invocation owns one decoder"],
                "lifecycle": {
                    "creation": "construct a decoder when decode starts",
                    "operation": "stream standard input through the decoder",
                    "shutdown": "finish the decoder at EOF",
                    "failure": "write a diagnostic and return nonzero",
                    "cleanup": "release command-local resources before exit",
                },
                "state_machine": None,
                "paths": {
                    "contract_mode": "review_guarded",
                    "contract_paths": ["include/framepipe/cli.hpp"],
                    "implementation_scopes": [
                        {"kind": "directory", "path": "src/cli"}
                    ],
                    "reference_only": [],
                },
            },
        },
        "scenarios": {
            "cli_decode_stream": {
                "modules": ["frame_protocol", "framepipe_cli"],
                "requirement_refs": ["decode_stream_completion"],
                "entrypoint": "framepipe decode",
                "contract_flow": [
                    "stdin -> framepipe_cli::decode",
                    "framepipe_cli -> frame_protocol::feed",
                    "frames and status -> stdout, stderr, and process exit",
                ],
                "observable_behavior": "prints complete decoded payloads in order",
                "failure_behavior": "incomplete input exits nonzero with a diagnostic",
                "environment": "project host running the built executable",
            }
        },
    }


def _comment_block(value: str) -> str:
    return "".join(f"# {line}\n" if line else "#\n" for line in value.strip().splitlines())


def _architecture_draft_template_header() -> str:
    example = ArchitectureDraft.model_validate(
        _architecture_draft_schema_example(),
        strict=True,
    ).model_dump(mode="python")
    example_yaml = yaml.safe_dump(
        example,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    return (
        _comment_block(_ARCHITECTURE_DRAFT_SCHEMA_GUIDANCE)
        + _comment_block(example_yaml)
        + _comment_block(_ARCHITECTURE_DRAFT_SCHEMA_GUIDANCE_END)
    )


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
    return _architecture_draft_template_header() + yaml.safe_dump(
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

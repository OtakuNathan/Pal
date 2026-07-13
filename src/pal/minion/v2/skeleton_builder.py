from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.skeleton import (
    MODULE_NAME_PATTERN,
    PATH_SCOPE_KINDS,
    SemanticReferenceError,
    validate_architecture_submission,
)
from pal.minion.workspace_tools import _append_unique_artifact, _write_minion_artifact
from pal.shared import RuntimeStatus


ARCHITECTURE_SKELETON_CAPABILITIES = ("op_minion_architecture_submit",)
SKELETON_REVIEW_CAPABILITIES = ("op_minion_skeleton_review_submit",)
SKELETON_BUILDER_CAPABILITIES = (*ARCHITECTURE_SKELETON_CAPABILITIES, *SKELETON_REVIEW_CAPABILITIES)

_REQUIREMENT_REF_SCHEMA = {
    "type": "object",
    "properties": {
        "section": {"type": "string", "minLength": 1},
        "requirement": {"type": "string", "minLength": 1},
    },
    "required": ["section", "requirement"],
    "additionalProperties": False,
}
_EVIDENCE_REF_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "workspace_file",
                "workspace_symbol",
                "reference_file",
                "reference_symbol",
                "documentation",
                "research_conclusion",
            ],
        },
        "path": {"type": "string"},
        "symbol": {"type": "string"},
        "reference_name": {"type": "string"},
        "source": {"type": "string"},
        "section": {"type": "string"},
        "conclusion": {"type": "string"},
    },
    "required": ["kind"],
    "additionalProperties": False,
}
_PATH_SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": sorted(PATH_SCOPE_KINDS)},
        "path": {"type": "string", "minLength": 1},
    },
    "required": ["kind", "path"],
    "additionalProperties": False,
}
_CONTRACT_REF_SCHEMA = {
    "type": "object",
    "properties": {
        "module": {"type": "string", "minLength": 1},
        "path": {"type": "string", "minLength": 1},
        "symbol": {"type": "string"},
    },
    "required": ["module", "path"],
    "additionalProperties": False,
}
_MODULE_PATHS_SCHEMA = {
    "type": "object",
    "properties": {
        "contract_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "implementation_scopes": {"type": "array", "items": _PATH_SCOPE_SCHEMA, "minItems": 1},
        "test_scopes": {"type": "array", "items": _PATH_SCOPE_SCHEMA, "minItems": 1},
        "reference_only": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["contract_paths", "implementation_scopes", "test_scopes", "reference_only"],
    "additionalProperties": False,
}
_MODULE_SCHEMA = {
    "type": "object",
    "properties": {
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "consumes": {"type": "array", "items": _CONTRACT_REF_SCHEMA},
        "paths": _MODULE_PATHS_SCHEMA,
        "covers": {"type": "array", "items": _REQUIREMENT_REF_SCHEMA, "minItems": 1},
        "evidence": {"type": "array", "items": _EVIDENCE_REF_SCHEMA},
    },
    "required": ["depends_on", "consumes", "paths", "covers", "evidence"],
    "additionalProperties": False,
}
_VERIFICATION_ENTRYPOINT_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["source_symbol", "build_target", "product_entrypoint", "platform_probe"],
        },
        "path": {"type": "string"},
        "symbol": {"type": "string"},
        "target": {"type": "string"},
    },
    "required": ["kind"],
    "additionalProperties": False,
}
_VERIFICATION_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["consumer_probe", "end_to_end", "dogfood", "platform"],
        },
        "depends_on": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "consumes": {"type": "array", "items": _CONTRACT_REF_SCHEMA, "minItems": 1},
        "covers": {"type": "array", "items": _REQUIREMENT_REF_SCHEMA, "minItems": 1},
        "entrypoints": {"type": "array", "items": _VERIFICATION_ENTRYPOINT_SCHEMA, "minItems": 1},
        "environment": {"type": "object"},
    },
    "required": ["kind", "depends_on", "consumes", "covers", "entrypoints", "environment"],
    "additionalProperties": False,
}
_LOCATION_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "symbol": {"type": "string"},
        "section": {"type": "string"},
    },
    "required": ["path"],
    "additionalProperties": False,
}


SKELETON_BUILDER_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_architecture_submit": {
        "name": "op_minion_architecture_submit",
        "description": (
            "Preflight and submit the complete semantic architecture index after writing the code skeleton. This is the Architect's only completion tool. "
            "modules is keyed by a unique snake_case semantic name. depends_on means ACCEPTED-before-start only; consumes separately names provider module, "
            "contract path, and optional symbol. contract_paths are frozen after Human Accept; the first is the primary contract entrypoint. "
            "implementation_scopes and test_scopes use only {kind:file|directory,path:...}; a directory must be an exclusive private namespace. "
            "verification_nodes describe real consumer, build, dogfood, or platform scenarios and the exact module candidates they combine; depends_on must "
            "include the complete Construction dependency closure for that scenario. Every hard "
            "Requirement needs a verification landing, but no synthetic all-module join is required. Evidence is optional and uses "
            "workspace_file|workspace_symbol|reference_file|reference_symbol|documentation|research_conclusion. The Manager preflights Requirement/Evidence "
            "references, all three graphs, path ownership, contract comments, and entrypoints before stopping the worker, then repeats validation on the "
            "stable snapshot. Do not include workflow IDs, "
            "revision IDs, requirement/evidence/finding IDs, artifact handles, SHA values, milestones, algorithms, implementation steps, or test matrices."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "modules": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": _MODULE_SCHEMA,
                },
                "verification_nodes": {
                    "type": "object",
                    "minProperties": 1,
                    "additionalProperties": _VERIFICATION_NODE_SCHEMA,
                },
            },
            "required": ["modules", "verification_nodes"],
            "additionalProperties": False,
        },
    },
    "op_minion_skeleton_review_submit": {
        "name": "op_minion_skeleton_review_submit",
        "description": (
            "Submit PASS or FAIL for the bound Requirements plus code skeleton. finding_kind is exactly requirements_defect|contract_defect|architecture_defect, "
            "and severity is exactly error|warning. Findings use semantic module names, original Requirement text, and source path/symbol/contract section only. "
            "Review Construction ordering, directional contract consumption, and real Verification Topology as separate graphs; no universal join is required. "
            "Do not invent IDs, handles, SHA values, JSON pointers, implementation details, or a replacement design. "
            "A FAIL must identify every material contract-level defect in one breadth-first pass."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
                "findings": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "finding_kind": {
                                "type": "string",
                                "enum": ["requirements_defect", "contract_defect", "architecture_defect"],
                            },
                            "summary": {"type": "string", "minLength": 1},
                            "severity": {"type": "string", "enum": ["error", "warning"]},
                            "affected_modules": {"type": "array", "items": {"type": "string"}},
                            "requirements": {"type": "array", "items": _REQUIREMENT_REF_SCHEMA},
                            "locations": {"type": "array", "items": _LOCATION_SCHEMA},
                        },
                        "required": [
                            "finding_kind",
                            "summary",
                            "severity",
                            "affected_modules",
                            "requirements",
                            "locations",
                        ],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["verdict", "findings"],
            "additionalProperties": False,
        },
    },
}


def is_skeleton_builder_capability(name: str) -> bool:
    return str(name or "") in SKELETON_BUILDER_TOOL_SPECS


def skeleton_builder_tool_result(
    call: CanonicalToolCall,
    workspace: dict[str, Any],
    produced_artifacts: list[dict[str, Any]],
) -> CanonicalToolResult:
    try:
        name = str(call.name or "")
        payload = dict(call.args or {})
        if name == "op_minion_architecture_submit":
            _validate_submission_shape(payload)
            _preflight_submission(payload, workspace)
            filename = "architecture_submission.json"
            title = "V2 architecture skeleton submission"
        elif name == "op_minion_skeleton_review_submit":
            _validate_review_shape(payload)
            filename = "architecture_review.json"
            title = "V2 architecture skeleton review"
        else:
            raise ValueError(f"unknown skeleton builder capability: {name}")
        artifact = _write_minion_artifact(
            workspace,
            {
                "relative_path": filename,
                "title": title,
                "role": "primary",
                "mime_type": "application/json",
                "overwrite": True,
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            },
        )
        _append_unique_artifact(produced_artifacts, artifact)
        return CanonicalToolResult(
            name=name,
            ok=True,
            text=f"{title} submitted",
            llm_text=f"{title} submitted",
            structured={"submitted": True, "artifact": artifact},
            call_id=call.call_id,
            status=RuntimeStatus.OK,
        )
    except Exception as exc:
        message = f"{exc.__class__.__name__}: {exc}"
        structured = (
            exc.to_dict()
            if isinstance(exc, SemanticReferenceError)
            else {"error": str(exc), "error_type": exc.__class__.__name__}
        )
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            llm_text=message,
            structured=structured,
            call_id=call.call_id,
            status=RuntimeStatus.INVALID,
        )


def _validate_submission_shape(payload: Mapping[str, Any]) -> None:
    modules = payload.get("modules")
    if not isinstance(modules, Mapping) or not modules:
        raise ValueError("modules must be a non-empty map")
    names = {str(name) for name in modules}
    for name, raw_module in modules.items():
        if MODULE_NAME_PATTERN.fullmatch(str(name)) is None:
            raise ValueError(f"invalid semantic module name: {name}")
        module = dict(raw_module or {})
        missing = {"depends_on", "consumes", "paths", "covers", "evidence"} - set(module)
        if missing:
            raise ValueError(f"module {name} is missing: {', '.join(sorted(missing))}")
        unknown = set(str(item) for item in list(module.get("depends_on") or [])) - names
        if unknown:
            raise ValueError(f"module {name} references unknown dependencies: {', '.join(sorted(unknown))}")
        paths = dict(module.get("paths") or {})
        if not list(paths.get("contract_paths") or []):
            raise ValueError(f"module {name} requires paths.contract_paths")
        for field in ("implementation_scopes", "test_scopes"):
            if not list(paths.get(field) or []):
                raise ValueError(f"module {name} requires paths.{field}")
    _assert_acyclic({str(name): [str(item) for item in list(dict(module).get("depends_on") or [])] for name, module in modules.items()})
    verification_nodes = payload.get("verification_nodes")
    if not isinstance(verification_nodes, Mapping) or not verification_nodes:
        raise ValueError("verification_nodes must be a non-empty map")


def _preflight_submission(payload: Mapping[str, Any], workspace: Mapping[str, Any]) -> None:
    references = {
        str(item.get("name") or ""): Path(str(item.get("path") or ""))
        for item in list(workspace.get("reference_paths") or [])
        if str(item.get("name") or "") and str(item.get("path") or "")
    }
    requirements_path = references.pop("requirements", None)
    if requirements_path is None or not requirements_path.is_file():
        raise ValueError("bound RequirementsArtifact is unavailable for architecture preflight")
    requirements = json.loads(requirements_path.read_text(encoding="utf-8"))
    evidence_path = references.pop("evidence_catalog", None)
    evidence = (
        json.loads(evidence_path.read_text(encoding="utf-8"))
        if evidence_path is not None and evidence_path.is_file()
        else None
    )
    repo_path = Path(str(workspace.get("repo_path") or "")).expanduser()
    if not repo_path.is_dir():
        raise ValueError("architecture worktree is unavailable for architecture preflight")
    validate_architecture_submission(
        payload,
        requirements_payload=requirements,
        workspace_root=repo_path,
        reference_roots=references,
        evidence_catalog=evidence,
    )


def _validate_review_shape(payload: Mapping[str, Any]) -> None:
    verdict = str(payload.get("verdict") or "")
    findings = list(payload.get("findings") or [])
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("verdict must be PASS or FAIL")
    if verdict == "PASS" and findings:
        raise ValueError("PASS cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL requires findings")
    for index, raw in enumerate(findings):
        finding = dict(raw or {})
        if not str(finding.get("summary") or "").strip():
            raise ValueError(f"finding {index} requires summary")
        if str(finding.get("finding_kind") or "") not in {
            "requirements_defect",
            "contract_defect",
            "architecture_defect",
        }:
            raise ValueError(f"finding {index} has invalid finding_kind")


def _assert_acyclic(depends_on: Mapping[str, list[str]]) -> None:
    pending = {name: set(values) for name, values in depends_on.items()}
    while pending:
        ready = {name for name, values in pending.items() if not values}
        if not ready:
            raise ValueError("module dependency graph contains a cycle: " + ", ".join(sorted(pending)))
        pending = {name: values - ready for name, values in pending.items() if name not in ready}

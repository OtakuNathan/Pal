from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.submission_preflight import (
    bound_reference_payload,
    requirement_refs_from_view,
)
from pal.minion.v2.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.shared import RuntimeStatus


async def run_shell_evidence(
    call: CanonicalToolCall,
    *,
    workspace: Mapping[str, Any],
    original_adapter: Any,
    draft_kind: str,
    case_kind: str,
    obligation_tag: str,
    turn_id: str | None = None,
) -> CanonicalToolResult:
    try:
        args = bind_semantic_case_arguments(
            dict(call.args or {}), workspace=workspace, draft_kind=draft_kind
        )
        name = _required_text(args, "name")
        command = _required_text(args, "command")
        description = str(args.get("description") or name).strip()
        timeout_seconds = max(1, int(args.get("timeout_seconds") or 300))
        expected_exit_codes = _integer_list(args.get("expected_exit_codes") or [0])
        context = SubmissionDraftContext.from_workspace(workspace, draft_kind=draft_kind)
        store = SubmissionDraftStore(_runtime_root(workspace))
        snapshot = store.read(context, seed=_empty_payload())
        existing = dict(dict(snapshot.payload.get("evidence") or {}).get("cases") or {}).get(name)
        request_fingerprint = _fingerprint_payload(
            context,
            {
                "arguments": args,
                "case_kind": case_kind,
                "obligation_tag": obligation_tag,
                "scratch_probe_fingerprint": scratch_probe_fingerprint(
                    workspace, str(args.get("probe_path") or "")
                ),
                "workspace_fingerprint": execution_workspace_fingerprint(workspace),
            },
        )
        if isinstance(existing, Mapping) and str(existing.get("request_fingerprint") or "") == request_fingerprint:
            return _success_result(
                call,
                f"reused recorded {name}: {existing.get('status')}",
                {"reused": True, "case": dict(existing)},
            )
        cwd = str(workspace.get("repo_path") or "").strip() or None
        result = await original_adapter.execute_tool_async(
            CanonicalToolCall(
                name="op_exec_shell",
                args={
                    "cmd": command,
                    **({"cwd": cwd} if cwd else {}),
                    "timeout_ms": timeout_seconds * 1000,
                },
                call_id=call.call_id,
            ),
            allow_tools=True,
            turn_id=turn_id,
        )
        structured = dict(result.structured or {})
        exit_code = structured.get("returncode")
        stdout = str(structured.get("stdout") or "")
        stderr = str(structured.get("stderr") or result.text or "")
        if exit_code is None:
            status = "UNKNOWN"
        else:
            status = "PASS" if int(exit_code) in expected_exit_codes else "FAIL"
        artifacts = _artifact_store(workspace)
        stdout_ref = artifacts.put_bytes(
            stdout.encode("utf-8"),
            artifact_type="VerificationStdoutArtifact",
            media_type="text/plain",
            provenance={"role": context.role, "case": name},
        )
        stderr_ref = artifacts.put_bytes(
            stderr.encode("utf-8"),
            artifact_type="VerificationStderrArtifact",
            media_type="text/plain",
            provenance={"role": context.role, "case": name},
        )
        case = {
            "name": name,
            "case_kind": case_kind,
            "obligation_tags": [obligation_tag] if obligation_tag else [],
            "command": ["/bin/sh", "-lc", command],
            "expected_exit_codes": expected_exit_codes,
            "description": description,
            "requirements": _single_requirement(args),
            "locations": _single_location(args),
            "invariants": _string_list(args.get("invariants") or []),
            "status": status,
            "exit_code": int(exit_code) if exit_code is not None else None,
            "stdout_ref": stdout_ref.to_dict(),
            "stderr_ref": stderr_ref.to_dict(),
            "environment": {"cwd": cwd or "", "runner": "scoped_shell"},
            "summary": f"exit {exit_code}; expected {expected_exit_codes}" if exit_code is not None else stderr[:500],
            "request_fingerprint": request_fingerprint,
            "input_fingerprint": context.input_fingerprint,
        }

        def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
            recorded = _upsert_recorded_case(payload, name=name, case=case)
            return payload, {"recorded": True, "case": recorded}

        mutation = store.mutate(
            context,
            operation_key=_operation_key(call, request_fingerprint),
            request=args,
            reducer=reducer,
            seed=_empty_payload(),
        )
        return _success_result(call, f"recorded {name}: {status}", mutation)
    except Exception as exc:
        return _error_result(call, exc)


async def run_lsp_evidence(
    call: CanonicalToolCall,
    *,
    workspace: Mapping[str, Any],
    original_adapter: Any,
    draft_kind: str,
    obligation_tag: str = "lsp",
    turn_id: str | None = None,
) -> CanonicalToolResult:
    try:
        args = bind_semantic_case_arguments(
            dict(call.args or {}), workspace=workspace, draft_kind=draft_kind
        )
        name = _required_text(args, "name")
        file_path = _required_text(args, "file")
        context = SubmissionDraftContext.from_workspace(workspace, draft_kind=draft_kind)
        store = SubmissionDraftStore(_runtime_root(workspace))
        snapshot = store.read(context, seed=_empty_payload())
        existing = dict(dict(snapshot.payload.get("evidence") or {}).get("cases") or {}).get(name)
        request_fingerprint = _fingerprint_payload(
            context,
            {
                "arguments": args,
                "tool": "lsp",
                "obligation_tag": obligation_tag,
                "workspace_fingerprint": execution_workspace_fingerprint(workspace),
            },
        )
        if isinstance(existing, Mapping) and str(existing.get("request_fingerprint") or "") == request_fingerprint:
            return _success_result(
                call,
                f"reused recorded {name}: {existing.get('status')}",
                {"reused": True, "case": dict(existing)},
            )
        result = await original_adapter.execute_tool_async(
            CanonicalToolCall(
                name="op_lsp_diagnostics",
                args={
                    "file": file_path,
                    **({"workspace_root": str(workspace.get("repo_path"))} if workspace.get("repo_path") else {}),
                },
                call_id=call.call_id,
            ),
            allow_tools=True,
            turn_id=turn_id,
        )
        serialized = json.dumps(dict(result.structured or {}), ensure_ascii=False, sort_keys=True)
        artifacts = _artifact_store(workspace)
        stdout_ref = artifacts.put_bytes(
            serialized.encode("utf-8"),
            artifact_type="LspDiagnosticsArtifact",
            media_type="application/json",
            provenance={"role": context.role, "case": name},
        )
        stderr_ref = artifacts.put_bytes(
            ("" if result.ok else str(result.text or result.llm_text or "")).encode("utf-8"),
            artifact_type="VerificationStderrArtifact",
            media_type="text/plain",
        )
        diagnostics = list(dict(result.structured or {}).get("diagnostics") or [])
        diagnostics_state = str(dict(result.structured or {}).get("diagnostics_state") or "")
        has_error = any(_diagnostic_is_error(item) for item in diagnostics)
        status = (
            "UNKNOWN"
            if not result.ok or diagnostics_state == "timed_out"
            else "FAIL"
            if has_error
            else "PASS"
        )
        case = {
            "name": name,
            "case_kind": "lsp",
            "obligation_tags": [obligation_tag],
            "command": ["<lsp-diagnostics>", file_path],
            "expected_exit_codes": [0],
            "description": str(args.get("description") or f"LSP diagnostics for {file_path}"),
            "requirements": _single_requirement(args),
            "locations": [{"path": file_path}],
            "invariants": [],
            "status": status,
            "exit_code": 1 if status == "FAIL" else 0 if status == "PASS" else None,
            "stdout_ref": stdout_ref.to_dict(),
            "stderr_ref": stderr_ref.to_dict(),
            "environment": {"workspace_root": str(workspace.get("repo_path") or ""), "runner": "lsp"},
            "summary": str(result.text or result.llm_text or status)[:500],
            "request_fingerprint": request_fingerprint,
            "input_fingerprint": context.input_fingerprint,
        }

        def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
            recorded = _upsert_recorded_case(payload, name=name, case=case)
            return payload, {"recorded": True, "case": recorded}

        mutation = store.mutate(
            context,
            operation_key=_operation_key(call, request_fingerprint),
            request=args,
            reducer=reducer,
            seed=_empty_payload(),
        )
        return _success_result(call, f"recorded {name}: {status}", mutation)
    except Exception as exc:
        return _error_result(call, exc)


def record_unavailable_evidence(
    call: CanonicalToolCall,
    *,
    workspace: Mapping[str, Any],
    draft_kind: str,
) -> CanonicalToolResult:
    try:
        args = bind_semantic_case_arguments(
            dict(call.args or {}), workspace=workspace, draft_kind=draft_kind
        )
        name = _required_text(args, "name")
        reason = _required_text(args, "reason")
        obligation = _required_text(args, "obligation")
        context = SubmissionDraftContext.from_workspace(workspace, draft_kind=draft_kind)
        store = SubmissionDraftStore(_runtime_root(workspace))
        case = {
            "name": name,
            "case_kind": "platform_assumption",
            "obligation_tags": [obligation],
            "command": ["<unavailable>", obligation],
            "expected_exit_codes": [0],
            "description": reason,
            "requirements": _single_requirement(args),
            "locations": _single_location(args),
            "invariants": [],
            "status": "UNKNOWN",
            "exit_code": None,
            "stdout_ref": {},
            "stderr_ref": {},
            "environment": {"runner": "unavailable"},
            "summary": reason,
            "request_fingerprint": _fingerprint_payload(context, args),
            "input_fingerprint": context.input_fingerprint,
        }

        def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
            recorded = _upsert_recorded_case(payload, name=name, case=case)
            return payload, {"recorded": True, "case": recorded}

        mutation = store.mutate(
            context,
            operation_key=_operation_key(call, case["request_fingerprint"]),
            request=args,
            reducer=reducer,
            seed=_empty_payload(),
        )
        return _success_result(call, f"recorded unavailable check {name}", mutation)
    except Exception as exc:
        return _error_result(call, exc)


def recorded_cases(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    cases = dict(dict(payload.get("evidence") or {}).get("cases") or {})
    recorded = [dict(item) for item in cases.values()]
    if recorded and all(_recorded_sequence(item) > 0 for item in recorded):
        return sorted(recorded, key=lambda item: (_recorded_sequence(item), str(item.get("name") or "")))
    return sorted(
        recorded,
        key=lambda item: (
            _CASE_KIND_ORDER.get(str(item.get("case_kind") or ""), 99),
            str(item.get("name") or ""),
        ),
    )


_CASE_KIND_ORDER = {
    "historical_regression": 0,
    "contract_adversarial": 1,
    "diff_risk": 2,
    "compile": 3,
    "lsp": 3,
    "unit": 3,
    "consumer_probe": 3,
    "platform_assumption": 4,
}


def _upsert_recorded_case(
    payload: dict[str, Any],
    *,
    name: str,
    case: Mapping[str, Any],
) -> dict[str, Any]:
    evidence = dict(payload.get("evidence") or {})
    cases = dict(evidence.get("cases") or {})
    previous = dict(cases.get(name) or {})
    recorded_sequence = _recorded_sequence(previous)
    if recorded_sequence <= 0:
        recorded_sequence = max(
            (_recorded_sequence(dict(item)) for item in cases.values()),
            default=0,
        ) + 1
    recorded = {**dict(case), "recorded_sequence": recorded_sequence}
    cases[name] = recorded
    evidence["cases"] = cases
    payload["evidence"] = evidence
    if str(recorded.get("status") or "") == "PASS":
        payload["findings"] = [
            dict(item)
            for item in list(payload.get("findings") or [])
            if str(dict(item).get("case") or "") != name
        ]
    return recorded


def _recorded_sequence(value: Mapping[str, Any]) -> int:
    sequence = value.get("recorded_sequence")
    return int(sequence) if type(sequence) is int and sequence > 0 else 0


def scratch_fingerprint(workspace: Mapping[str, Any]) -> str:
    root = Path(str(workspace.get("review_scratch_dir") or ""))
    digest = hashlib.sha256()
    if not root.is_dir():
        digest.update(b"<missing>")
        return digest.hexdigest()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def scratch_probe_fingerprint(workspace: Mapping[str, Any], probe_path: str) -> str:
    """Hash only the scratch input explicitly used by one semantic case."""

    raw_path = str(probe_path or "").strip()
    digest = hashlib.sha256()
    if not raw_path:
        digest.update(b"<none>")
        return digest.hexdigest()
    relative = PurePosixPath(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("probe_path must be a safe path relative to the verifier scratch directory")
    root = Path(str(workspace.get("review_scratch_dir") or ""))
    path = root / relative
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"probe_path does not name a verifier scratch file: {relative}")
    digest.update(relative.as_posix().encode("utf-8"))
    digest.update(b"\0")
    digest.update(path.read_bytes())
    return digest.hexdigest()


def bind_semantic_case_arguments(
    args: Mapping[str, Any],
    *,
    workspace: Mapping[str, Any],
    draft_kind: str,
) -> dict[str, Any]:
    """Bind a case's semantic Requirement before any execution or Draft mutation."""

    bound = dict(args)
    section = str(bound.get("requirement_section") or "").strip()
    hint = str(bound.get("requirement") or "").strip()
    if not section and hint:
        raise ValueError("requirement requires requirement_section")
    if not section:
        bound.pop("requirement", None)
        bound.pop("requirement_section", None)
        return bound
    view_name = "review_request" if draft_kind == "standalone_review" else "module_work_view"
    view = bound_reference_payload(workspace, view_name, required=False)
    if not view:
        if not hint:
            raise ValueError(
                "requirement is required when no bound Requirement catalog is available"
            )
        bound["requirement_section"] = section
        bound["requirement"] = hint
        return bound
    by_section: dict[str, list[str]] = {}
    for candidate_section, requirement in sorted(requirement_refs_from_view(view)):
        by_section.setdefault(candidate_section, []).append(requirement)
    candidates = by_section.get(section)
    if not candidates:
        rendered = ", ".join(sorted(by_section)) or "<none>"
        raise ValueError(
            f"Requirement section {section!r} is not bound. Available sections: {rendered}"
        )
    if len(candidates) == 1:
        canonical = candidates[0]
    elif hint in candidates:
        canonical = hint
    else:
        rendered = "; ".join(candidates)
        raise ValueError(
            f"Requirement section {section!r} is ambiguous; provide one exact canonical requirement. "
            f"Candidates: {rendered}"
        )
    bound["requirement_section"] = section
    bound["requirement"] = canonical
    return bound


def execution_workspace_fingerprint(workspace: Mapping[str, Any]) -> str:
    root_value = str(workspace.get("repo_path") or "").strip()
    digest = hashlib.sha256()
    if not root_value:
        digest.update(b"<unbound>")
        return digest.hexdigest()
    root = Path(root_value).expanduser()
    if not root.is_dir():
        digest.update(b"<missing>")
        digest.update(str(root).encode("utf-8"))
        return digest.hexdigest()
    if (root / ".git").exists():
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--binary", "HEAD", "--"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        untracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "-z"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if head.returncode == diff.returncode == untracked.returncode == 0:
            digest.update(head.stdout)
            digest.update(diff.stdout)
            for raw_path in sorted(item for item in untracked.stdout.split(b"\0") if item):
                relative = raw_path.decode("utf-8", errors="surrogateescape")
                path = root / relative
                digest.update(raw_path)
                digest.update(b"\0")
                if path.is_file() and not path.is_symlink():
                    digest.update(path.read_bytes())
                digest.update(b"\0")
            return digest.hexdigest()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and not item.is_symlink() and ".git" not in item.parts
    ):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _artifact_store(workspace: Mapping[str, Any]) -> ContentAddressedArtifactStore:
    root = _runtime_root(workspace)
    repository = MinionV2Repository(root)
    repository.ensure_schema()
    return ContentAddressedArtifactStore(root, repository)


def _runtime_root(workspace: Mapping[str, Any]) -> Path:
    value = str(workspace.get("runtime_root") or "").strip()
    if not value:
        raise ValueError("semantic evidence tool requires the bound runtime root")
    return Path(value)


def _single_requirement(args: Mapping[str, Any]) -> list[dict[str, str]]:
    section = str(args.get("requirement_section") or "").strip()
    requirement = str(args.get("requirement") or "").strip()
    if bool(section) != bool(requirement):
        raise ValueError("requirement_section and requirement must be provided together")
    return [{"section": section, "requirement": requirement}] if section else []


def _single_location(args: Mapping[str, Any]) -> list[dict[str, str]]:
    path = str(args.get("path") or "").strip()
    if not path:
        return []
    return [
        {
            "path": path,
            **({"symbol": str(args.get("symbol"))} if str(args.get("symbol") or "").strip() else {}),
            **({"section": str(args.get("contract_section"))} if str(args.get("contract_section") or "").strip() else {}),
        }
    ]


def _empty_payload() -> dict[str, Any]:
    return {"definitions": {}, "evidence": {"cases": {}}, "findings": [], "summary": {}}


def _required_text(args: Mapping[str, Any], field: str) -> str:
    value = str(args.get(field) or "").strip()
    if not value:
        raise ValueError(f"{field} is required")
    return value


def _integer_list(value: Any) -> list[int]:
    if not isinstance(value, list) or not value or any(type(item) is not int for item in value):
        raise ValueError("expected_exit_codes must be a non-empty integer array")
    return [int(item) for item in value]


def _diagnostic_is_error(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    severity = value.get("severity")
    return severity == 1 or str(severity or "").strip().lower() == "error"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("invariants must be a string array")
    return [str(item) for item in value if str(item).strip()]


def _fingerprint_payload(context: SubmissionDraftContext, value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        {"input_fingerprint": context.input_fingerprint, "value": dict(value)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _operation_key(call: CanonicalToolCall, fallback: str) -> str:
    return str(call.call_id or "").strip() or f"semantic:{call.name}:{fallback}"


def _success_result(call: CanonicalToolCall, text: str, structured: Mapping[str, Any]) -> CanonicalToolResult:
    return CanonicalToolResult(
        name=call.name,
        ok=True,
        text=text,
        llm_text=text,
        structured=dict(structured),
        call_id=call.call_id,
        status=RuntimeStatus.OK,
    )


def _error_result(call: CanonicalToolCall, exc: Exception) -> CanonicalToolResult:
    text = f"{exc.__class__.__name__}: {exc}"
    return CanonicalToolResult(
        name=call.name,
        ok=False,
        text=text,
        llm_text=text + " Correct only this local issue and retry the same semantic operation.",
        structured={"error": str(exc), "error_type": exc.__class__.__name__},
        call_id=call.call_id,
        status=RuntimeStatus.INVALID,
    )

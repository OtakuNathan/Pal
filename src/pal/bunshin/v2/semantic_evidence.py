from __future__ import annotations
from pal.bunshin.v2.draft_values import (recorded_cases, _recorded_sequence)

from pal.bunshin.verifier_tool_diagnostics import record_verifier_failure

from pal.shared.tool_protocol import ToolCallIR
from pal.execution.tool_facade import rejection

from pal.shared.tool_protocol import new_tool_call

import json
import hashlib
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.verification_readiness import verification_corpus_snapshot, record_verification_execution, lsp_verification_status, shell_execution_output, bind_recorded_case_execution, case_corpus_binding, case_definition_fingerprint
from pal.bunshin.v2.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.shared import RuntimeStatus, ToolExecutionResult


async def run_shell_evidence(
    call: ToolCallIR,
    *,
    workspace: Mapping[str, Any],
    original_adapter: Any,
    draft_kind: str,
    case_kind: str,
    obligation_tag: str,
    turn_id: str | None = None,
) -> ToolExecutionResult:
    execution_started = False
    try:
        args = dict(call.args or {})
        try:
            name = _required_text(args, "name")
            command = _required_text(args, "command")
            description = str(args.get("description") or name).strip()
            timeout_seconds = max(1, int(args.get("timeout_seconds") or 300))
            expected_exit_codes = _integer_list(args.get("expected_exit_codes") or [0])
            probe_fingerprint = scratch_probe_fingerprint(workspace, str(args.get("probe_path") or ""))
        except ValueError as exc:
            return _error_result(call, exc, execution_started=False, input_rejected=True)
        context = SubmissionDraftContext.from_workspace(workspace, draft_kind=draft_kind)
        store = SubmissionDraftStore(_runtime_root(workspace))
        store.assert_editable(context)
        if call.call_id:
            replay = store.read_operation(context, operation_key=call.call_id, request=args)
            if replay is not None:
                return _success_result(call, "Replayed recorded operation; no fresh execution occurred.",
                                       {**replay, "replayed": True})
        snapshot = store.read(context, seed=_empty_payload())
        existing = dict(dict(snapshot.payload.get("evidence") or {}).get("cases") or {}).get(name)
        artifacts = _artifact_store(workspace)
        request_fingerprint = _fingerprint_payload(
            context,
            {
                "arguments": args,
                "case_kind": case_kind,
                "obligation_tag": obligation_tag,
                "scratch_probe_fingerprint": probe_fingerprint,
                "workspace_fingerprint": execution_workspace_fingerprint(workspace),
            },
        )
        if (
            isinstance(existing, Mapping)
            and str(existing.get("request_fingerprint") or "") == request_fingerprint
            and str(existing.get("status") or "") != "UNKNOWN"
            and int(existing.get("recorded_case_revision", -1)) == int(snapshot.payload.get("case_revision") or 0)
        ):
            stdout = _artifact_text(artifacts, existing.get("stdout_ref"))
            stderr = _artifact_text(artifacts, existing.get("stderr_ref"))
            execution = _shell_execution_projection(
                existing,
                stdout=stdout,
                stderr=stderr,
            )
            return _success_result(
                call,
                f"reused recorded {name}: {existing.get('status')}",
                {
                    "reused": True,
                    "case": dict(existing),
                    "execution": execution,
                },
                llm_text=_shell_execution_text(name, execution, reused=True),
            )
        cwd = str(workspace.get("repo_path") or "").strip() or None
        execution_call = new_tool_call(
            name="op_exec_shell", args={"cmd": command,
                **({"cwd": cwd} if cwd else {}), "timeout_ms": timeout_seconds * 1000},
            call_id=call.call_id,
        )
        before = verification_corpus_snapshot(workspace)
        execution_started = True
        result = await original_adapter.execute_tool_async(
            execution_call,
            allow_tools=True,
            turn_id=turn_id,
        )
        record_verification_execution(workspace, execution_call, result, before)
        structured = shell_execution_output(result)
        exit_code = structured.get("returncode")
        stdout = str(structured.get("stdout") or "")
        stderr = str(structured.get("stderr") or result.text or "")
        if (type(exit_code) is not int or structured.get("timed_out") or structured.get("cancelled")
                or structured.get("_runtime_error_code") not in {None, "command_failed"}
                or (not result.ok and exit_code == 0)):
            status = "UNKNOWN"
        else:
            status = "PASS" if int(exit_code) in expected_exit_codes else "FAIL"
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
            "requirements": [],
            "locations": _single_location(args),
            "invariants": _string_list(args.get("invariants") or []),
            "status": status,
            "exit_code": exit_code if type(exit_code) is int else None,
            "stdout_ref": stdout_ref.to_dict(),
            "stderr_ref": stderr_ref.to_dict(),
            "environment": {"cwd": cwd or "", "runner": "scoped_shell"},
            "summary": f"exit {exit_code}; expected {expected_exit_codes}" if exit_code is not None else stderr[:500],
            "request_fingerprint": request_fingerprint,
            "recorded_case_revision": (int(snapshot.payload.get("case_revision") or 0) + 1
                                       if context.role == "verifier" else 0),
            "execution_id": str(call.call_id or request_fingerprint),
            "case_binding": case_corpus_binding(before),
            "input_fingerprint": context.input_fingerprint,
        }

        mutation = _commit_executed_case(
            store, context, snapshot, call, args, case, workspace, before, request_fingerprint,
        )
        execution = _shell_execution_projection(
            case,
            stdout=stdout,
            stderr=stderr,
        )
        return _success_result(
            call,
            f"recorded {name}: {status}",
            {**mutation, "execution": execution},
            llm_text=_shell_execution_text(name, execution),
        )
    except Exception as exc:
        return _error_result(call, exc, execution_started=execution_started)


async def run_lsp_evidence(
    call: ToolCallIR,
    *,
    workspace: Mapping[str, Any],
    original_adapter: Any,
    draft_kind: str,
    obligation_tag: str = "lsp",
    turn_id: str | None = None,
) -> ToolExecutionResult:
    execution_started = False
    try:
        args = dict(call.args or {})
        try:
            name = _required_text(args, "name")
            file_path = _required_text(args, "file")
        except ValueError as exc:
            return _error_result(call, exc, execution_started=False, input_rejected=True)
        lsp_environment_fingerprint = str(
            workspace.get("lsp_environment_fingerprint") or ""
        ).strip()
        context = SubmissionDraftContext.from_workspace(workspace, draft_kind=draft_kind)
        store = SubmissionDraftStore(_runtime_root(workspace))
        store.assert_editable(context)
        if call.call_id:
            replay = store.read_operation(context, operation_key=call.call_id, request=args)
            if replay is not None:
                return _success_result(call, "Replayed recorded operation; no fresh execution occurred.",
                                       {**replay, "replayed": True})
        snapshot = store.read(context, seed=_empty_payload())
        existing = dict(dict(snapshot.payload.get("evidence") or {}).get("cases") or {}).get(name)
        artifacts = _artifact_store(workspace)
        request_fingerprint = _fingerprint_payload(
            context,
            {
                "arguments": args,
                "tool": "lsp",
                "obligation_tag": obligation_tag,
                "workspace_fingerprint": execution_workspace_fingerprint(workspace),
                "lsp_environment_fingerprint": lsp_environment_fingerprint,
            },
        )
        if (
            isinstance(existing, Mapping)
            and str(existing.get("request_fingerprint") or "") == request_fingerprint
            and str(existing.get("status") or "") != "UNKNOWN"
            and int(existing.get("recorded_case_revision", -1)) == int(snapshot.payload.get("case_revision") or 0)
        ):
            recorded_result = _artifact_json(artifacts, existing.get("stdout_ref"))
            return _success_result(
                call,
                f"reused recorded {name}: {existing.get('status')}",
                {
                    "reused": True,
                    "case": dict(existing),
                    "execution": recorded_result,
                },
                llm_text=_lsp_execution_text(
                    name,
                    str(existing.get("status") or ""),
                    recorded_result,
                    reused=True,
                ),
            )
        execution_call = new_tool_call(
            name="op_lsp_diagnostics", args={"file": file_path,
                **({"workspace_root": str(workspace.get("repo_path"))} if workspace.get("repo_path") else {})},
            call_id=call.call_id,
        )
        before = verification_corpus_snapshot(workspace)
        execution_started = True
        result = await original_adapter.execute_tool_async(
            execution_call,
            allow_tools=True,
            turn_id=turn_id,
        )
        record_verification_execution(workspace, execution_call, result, before)
        serialized = json.dumps(dict(result.structured or {}), ensure_ascii=False, sort_keys=True)
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
        structured = dict(result.structured or {})
        status = lsp_verification_status(result)
        lsp_evidence = (
            dict(structured.get("evidence") or {})
            if isinstance(structured.get("evidence"), Mapping)
            else {}
        )
        case = {
            "name": name,
            "case_kind": "lsp",
            "obligation_tags": [obligation_tag],
            "command": ["<lsp-diagnostics>", file_path],
            "expected_exit_codes": [0],
            "description": str(args.get("description") or f"LSP diagnostics for {file_path}"),
            "requirements": [],
            "locations": [{"path": file_path}],
            "invariants": [],
            "status": status,
            "exit_code": 1 if status == "FAIL" else 0 if status == "PASS" else None,
            "stdout_ref": stdout_ref.to_dict(),
            "stderr_ref": stderr_ref.to_dict(),
            "environment": {
                "workspace_root": str(workspace.get("repo_path") or ""),
                "runner": "lsp",
                "primary_language": str(workspace.get("primary_language") or ""),
                "environment_fingerprint": str(
                    lsp_evidence.get("environment_fingerprint")
                    or lsp_environment_fingerprint
                ),
            },
            "summary": str(result.text or result.llm_text or status)[:500],
            "request_fingerprint": request_fingerprint,
            "recorded_case_revision": (int(snapshot.payload.get("case_revision") or 0) + 1
                                       if context.role == "verifier" else 0),
            "execution_id": str(call.call_id or request_fingerprint),
            "case_binding": case_corpus_binding(before),
            "input_fingerprint": context.input_fingerprint,
        }

        mutation = _commit_executed_case(
            store, context, snapshot, call, args, case, workspace, before, request_fingerprint,
        )
        return _success_result(
            call,
            f"recorded {name}: {status}",
            {**mutation, "execution": structured},
            llm_text=_lsp_execution_text(name, status, structured),
        )
    except Exception as exc:
        return _error_result(call, exc, execution_started=execution_started)


def record_unavailable_evidence(
    call: ToolCallIR,
    *,
    workspace: Mapping[str, Any],
    draft_kind: str,
) -> ToolExecutionResult:
    try:
        args = dict(call.args or {})
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
            "requirements": [],
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


def _commit_executed_case(store, context, snapshot, call, args, case, workspace, before, request_fingerprint):
    case["definition_fingerprint"] = case_definition_fingerprint(case)

    def reducer(payload: dict[str, Any]) -> tuple[dict[str, Any], Mapping[str, Any]]:
        recorded = _upsert_recorded_case(payload, name=str(case["name"]), case=case)
        return payload, {"recorded": True, "case": recorded}

    mutation = store.mutate(
        context,
        operation_key=_operation_key(call, request_fingerprint),
        request=args,
        reducer=reducer,
        seed=_empty_payload(),
        expected_version=snapshot.version,
    )
    bind_recorded_case_execution(workspace, before, int(mutation.get("case_revision") or 0))
    return mutation


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


def scratch_fingerprint(workspace: Mapping[str, Any]) -> str:
    root = Path(str(workspace.get("review_scratch_dir") or ""))
    digest = hashlib.sha256()
    if not root.is_dir():
        digest.update(b"<missing>")
        return digest.hexdigest()
    for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            # Scratch or database sidecar files may be removed between discovery
            # and reading. They were never stable evidence for this snapshot.
            continue
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
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
                try:
                    content = (
                        path.read_bytes()
                        if path.is_file() and not path.is_symlink()
                        else b""
                    )
                except FileNotFoundError:
                    continue
                digest.update(raw_path)
                digest.update(b"\0")
                digest.update(content)
                digest.update(b"\0")
            return digest.hexdigest()
    for path in sorted(
        item
        for item in root.rglob("*")
        if item.is_file() and not item.is_symlink() and ".git" not in item.parts
    ):
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            # Runtime roots commonly contain transient SQLite WAL/SHM files.
            # A file that disappears before it can be read is outside the
            # observable workspace snapshot and should not fail verification.
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def _artifact_store(workspace: Mapping[str, Any]) -> Any:
    root = _runtime_root(workspace)
    from pal.bunshin.v2.role_gateway_client import (
        RoleGatewayArtifactStore,
        role_gateway_client_from_env,
    )

    gateway = role_gateway_client_from_env(root)
    if gateway is not None:
        return RoleGatewayArtifactStore(gateway)
    repository = BunshinV2Repository(root)
    repository.database.ensure_schema()
    return ContentAddressedArtifactStore(root, repository.artifacts)


def _runtime_root(workspace: Mapping[str, Any]) -> Path:
    value = str(workspace.get("runtime_root") or "").strip()
    if not value:
        raise ValueError("semantic evidence tool requires the bound runtime root")
    return Path(value)


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


def _operation_key(call: ToolCallIR, fallback: str) -> str:
    return str(call.call_id or "").strip() or f"semantic:{call.name}:{fallback}"


def _artifact_text(
    artifacts: ContentAddressedArtifactStore,
    ref: Any,
) -> str:
    if not isinstance(ref, (Mapping, str)) or not ref:
        return ""
    return artifacts.read_bytes(ref).decode("utf-8", errors="replace")


def _artifact_json(
    artifacts: ContentAddressedArtifactStore,
    ref: Any,
) -> dict[str, Any]:
    raw = _artifact_text(artifacts, ref)
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, Mapping):
        raise ValueError("recorded LSP evidence is not a JSON object")
    return dict(value)


def _shell_execution_projection(
    case: Mapping[str, Any],
    *,
    stdout: str,
    stderr: str,
) -> dict[str, Any]:
    return {
        "status": str(case.get("status") or "UNKNOWN"),
        "exit_code": case.get("exit_code"),
        "expected_exit_codes": [
            int(item) for item in list(case.get("expected_exit_codes") or [])
        ],
        "stdout": stdout,
        "stderr": stderr,
    }


def _shell_execution_text(
    name: str,
    execution: Mapping[str, Any],
    *,
    reused: bool = False,
) -> str:
    prefix = "Reused" if reused else "Recorded"
    lines = [
        (
            f"{prefix} {name}: {execution.get('status')}; "
            f"exit_code={execution.get('exit_code')}; "
            f"expected_exit_codes={list(execution.get('expected_exit_codes') or [])}"
        ),
        "",
        "stdout:",
        str(execution.get("stdout") or "(empty)"),
        "",
        "stderr:",
        str(execution.get("stderr") or "(empty)"),
    ]
    return "\n".join(lines)


def _lsp_execution_text(
    name: str,
    status: str,
    execution: Mapping[str, Any],
    *,
    reused: bool = False,
) -> str:
    prefix = "Reused" if reused else "Recorded"
    return (
        f"{prefix} {name}: {status}\n\n"
        + json.dumps(dict(execution), ensure_ascii=False, indent=2, sort_keys=True)
    )


def _success_result(
    call: ToolCallIR,
    text: str,
    structured: Mapping[str, Any],
    *,
    llm_text: str | None = None,
) -> ToolExecutionResult:
    return ToolExecutionResult(
        name=call.name,
        ok=True,
        text=text,
        llm_text=str(llm_text or text),
        structured=dict(structured),
        call_id=call.call_id,
        status=RuntimeStatus.OK,
    )


def _error_result(call: ToolCallIR, exc: Exception, *, execution_started: bool = True, input_rejected: bool = False) -> ToolExecutionResult:
    record_verifier_failure(exc)
    text = f"{exc.__class__.__name__}: {exc}"
    return ToolExecutionResult(
        name=call.name,
        ok=False,
        text=text,
        llm_text=text + (" Execution may have started; reconcile before retrying." if execution_started
                         else " Correct the pre-execution issue before retrying."),
        structured={"error": str(exc), "error_type": exc.__class__.__name__},
        call_id=call.call_id,
        status=RuntimeStatus.INVALID,
        invocation_result=(rejection("verification_preflight", text + " No command ran. Correct the input and retry.",
                                     details={"error": str(exc), "error_type": type(exc).__name__})
                           if input_rejected and not isinstance(exc.__cause__, OSError) else None),
    )

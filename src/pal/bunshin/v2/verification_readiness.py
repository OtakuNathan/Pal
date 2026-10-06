"""Content-bound verifier execution receipts (never authored by tool callers)."""
from __future__ import annotations

import hashlib
import os
import subprocess
import stat
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from pal.shared.tool_protocol import FailedResult


def shell_execution_output(result: Any) -> dict[str, Any]:
    """Unwrap the actual runtime's failed-command envelope, not stdout JSON."""
    if isinstance(result.invocation_result, FailedResult):
        return {**dict(result.invocation_result.details),
                "_runtime_error_code": result.invocation_result.error_code}
    return dict(result.structured or {})


def verification_case_errors(
    cases: list[dict[str, Any]], *, outcome: str, workspace: Mapping[str, Any] | None,
) -> list[str]:
    """Shared local/Manager status and assignment-freshness gate."""
    errors = []
    if outcome == "pass":
        unresolved = [str(item.get("name") or "") for item in cases
                      if item.get("status") in {"FAIL", "UNKNOWN"}]
        if unresolved:
            errors.append("PASS requires resolved PASS evidence; failed or UNKNOWN cases: " + ", ".join(unresolved))
    if workspace is not None:
        current_input = str(dict(workspace.get("bunshin_v2") or {}).get("authoring_input_fingerprint") or "")
        stale = [str(item.get("name") or "") for item in cases
                 if item.get("input_fingerprint") and item["input_fingerprint"] != current_input]
        if stale:
            errors.append("rerun evidence for the current Candidate: " + ", ".join(stale))
    return errors


def verification_corpus_snapshot(workspace: Mapping[str, Any]) -> dict[str, Any]:
    """Snapshot explicit corpus scopes with bounded, no-follow descriptor reads.

    Relative paths agree across Manager/sandbox mounts. Races, unreadable files,
    and exceeded limits fail closed; no partial digest can qualify a receipt.
    """
    binding = dict(workspace.get("bunshin_v2") or {})
    root_value = str(workspace.get("repo_path") or "")
    root = Path(root_value).resolve() if root_value else None
    candidate = ""
    if root is not None and (root / ".git").exists():
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=5,
        )
        candidate = head.stdout.strip()
    entries: dict[str, str] = {}
    count = total_bytes = 0
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK

    def identity(info: os.stat_result) -> tuple[int, ...]:
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def collect(parent: int, name: str, relative: str, depth: int = 0, kind: str = "") -> None:
        nonlocal count, total_bytes
        count += 1
        if count > 10000 or depth > 64:
            raise ValueError("verification corpus snapshot exceeds its file/depth limit")
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            entries[relative] = "missing"
            return
        if stat.S_ISLNK(before.st_mode):
            entries[relative] = "symlink:" + os.readlink(name, dir_fd=parent)
        elif kind == "file" and not stat.S_ISREG(before.st_mode):
            entries[relative] = "not-a-file"
        elif kind == "directory" and not stat.S_ISDIR(before.st_mode):
            entries[relative] = "not-a-directory"
        elif stat.S_ISREG(before.st_mode):
            total_bytes += before.st_size
            if total_bytes > 256 * 1024 * 1024:
                raise ValueError("verification corpus snapshot exceeds its byte limit")
            with os.fdopen(os.open(name, flags, dir_fd=parent), "rb") as stream:
                if identity(os.fstat(stream.fileno())) != identity(before):
                    raise ValueError("verification corpus changed during snapshot")
                digest = hashlib.sha256()
                remaining = before.st_size
                while True:
                    chunk = stream.read(min(1024 * 1024, remaining + 1))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise ValueError("verification corpus changed during snapshot")
                    digest.update(chunk)
                entries[relative] = digest.hexdigest()
                if identity(os.fstat(stream.fileno())) != identity(before):
                    raise ValueError("verification corpus changed during snapshot")
        elif stat.S_ISDIR(before.st_mode):
            fd = os.open(name, flags | os.O_DIRECTORY, dir_fd=parent)
            try:
                if identity(os.fstat(fd)) != identity(before):
                    raise ValueError("verification corpus changed during snapshot")
                with os.scandir(fd) as children:
                    for child in children:
                        collect(fd, child.name, f"{relative}/{child.name}".strip("/"), depth + 1)
            finally:
                os.close(fd)
        else:
            raise ValueError("verification corpus contains an unsupported file type")
        if identity(os.stat(name, dir_fd=parent, follow_symlinks=False)) != identity(before):
            raise ValueError("verification corpus changed during snapshot")

    if workspace.get("verification_scratch_only"):
        scratch = str(workspace.get("review_scratch_dir") or "")
        if scratch:
            path = Path(scratch)
            parent = os.open(path.parent, flags | os.O_DIRECTORY)
            try:
                collect(parent, path.name, "review_scratch", kind="directory")
            finally:
                os.close(parent)
    elif root is not None:
        root_fd = os.open(root, flags | os.O_DIRECTORY)
        try:
            for raw in list(workspace.get("write_path_scopes") or []):
                scope = dict(raw or {})
                relative = PurePosixPath(str(scope.get("path") or ""))
                if (str(relative) in {"", "."} or relative.is_absolute()
                        or ".." in relative.parts or scope.get("kind") not in {"file", "directory"}):
                    raise ValueError("verification corpus requires a safe bound path scope")
                parent = os.dup(root_fd)
                ancestors = []
                try:
                    for part in relative.parts[:-1]:
                        try:
                            info = os.stat(part, dir_fd=parent, follow_symlinks=False)
                        except FileNotFoundError:
                            entries[str(relative)] = "missing"
                            break
                        if stat.S_ISLNK(info.st_mode):
                            entries[str(relative)] = "symlink:" + os.readlink(part, dir_fd=parent)
                            break
                        next_fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
                        if identity(os.fstat(next_fd)) != identity(info):
                            os.close(next_fd)
                            raise ValueError("verification corpus changed during snapshot")
                        ancestors.append((parent, part, info))
                        parent = next_fd
                    else:
                        collect(parent, relative.name, str(relative), kind=scope["kind"])
                    for ancestor_fd, part, before in ancestors:
                        if identity(os.stat(part, dir_fd=ancestor_fd, follow_symlinks=False)) != identity(before):
                            raise ValueError("verification corpus changed during snapshot")
                finally:
                    os.close(parent)
                    for ancestor_fd, _, _ in ancestors:
                        os.close(ancestor_fd)
        finally:
            os.close(root_fd)
    return {"input_fingerprint": str(binding.get("authoring_input_fingerprint") or ""),
            "candidate_digest": candidate,
            "corpus": [[name, digest] for name, digest in sorted(entries.items())]}


def lsp_verification_status(result: Any) -> str:
    """A completed RPC with missing/version-unknown diagnostics is not PASS."""
    output = dict(result.structured or {})
    data = dict(output.get("result") or output)
    evidence = dict(output.get("evidence") or {})
    if (not result.ok or output.get("operation") not in {None, "diagnostics"}
            or str(output.get("status") or "") not in {"", "ok"}
            or str(data.get("status") or "") not in {"", "ok"}
            or data.get("diagnostics_state") != "fresh"
            or evidence.get("freshness") not in {None, "fresh"}
            or output.get("cancelled") or data.get("cancelled")
            or output.get("timed_out") or data.get("timed_out")
            or not isinstance(data.get("diagnostics"), list)
            or any(not isinstance(item, Mapping) for item in data.get("diagnostics", []))):
        return "UNKNOWN"
    return "FAIL" if any(isinstance(item, Mapping)
                        and str(item.get("severity") or "").lower() in {"1", "error"}
                        for item in data["diagnostics"]) else "PASS"


def current_verification_receipts(
    receipts: list[dict[str, Any]], workspace: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Invalidate bound receipts on content/assignment changes, without re-stamping."""
    current = verification_corpus_snapshot(workspace)
    result = []
    for item in receipts:
        receipt = dict(item)
        bound = receipt.get("verification_binding")
        # Legacy receipts remain readable history, but cannot establish which
        # Candidate/corpus ran. Only a new execution can supply that proof.
        if not isinstance(bound, Mapping) or dict(bound) != current:
            receipt["ok"] = False
            receipt["stale"] = True
        result.append(receipt)
    return result


def final_verification_errors(receipts: list[dict[str, Any]], *, outcome: str, changed: bool) -> list[str]:
    checks = final_verification_checks(receipts)
    errors = []
    if not checks and any(item.get("stale") for item in receipts):
        errors.append("fresh validation required: prior execution receipts do not prove the current Candidate and corpus")
    if changed and not checks:
        errors.append("run verification again after the final test edit")
    if outcome == "pass" and not any(bool(item.get("ok")) for item in checks):
        errors.append("PASS requires a successful final command or LSP receipt")
    return errors


def final_verification_checks(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    last_write = max((i for i, item in enumerate(receipts)
                      if item.get("kind") == "test_write"), default=-1)
    return [item for i, item in enumerate(receipts)
            if i > last_write and item.get("kind") in {"command", "lsp"}
            and not item.get("stale")]


def record_verification_execution(
    workspace: Mapping[str, Any], call: Any, result: Any,
    before: Mapping[str, Any],
) -> None:
    """Record the delegated execution, never a claimed receipt in its output."""
    from pal.bunshin.scoped_execution import _review_tool_evidence_ref

    receipt = _review_tool_evidence_ref(call.name, call, result)
    if not receipt:
        return
    receipt["verification_binding"] = dict(before)
    receipt["stale"] = dict(before) != verification_corpus_snapshot(workspace)
    # A handler completing is not proof that its command succeeded. In
    # particular expected nonzero semantic cases cannot manufacture final PASS.
    if receipt["kind"] == "command":
        output = shell_execution_output(result)
        receipt["ok"] = (bool(result.ok) and type(output.get("returncode")) is int and output["returncode"] == 0
                         and not output.get("timed_out") and not output.get("cancelled"))
    elif receipt["kind"] == "lsp":
        receipt["ok"] = call.name == "op_lsp_diagnostics" and lsp_verification_status(result) == "PASS"
    refs = workspace.get("review_tool_evidence_refs")
    if refs is None and isinstance(workspace, dict):
        refs = workspace.setdefault("review_tool_evidence_refs", [])
    if isinstance(refs, list):
        refs.append(receipt)

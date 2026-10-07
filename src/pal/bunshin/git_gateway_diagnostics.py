"""Bounded metadata for authenticated Git RPCs, never commands or output logs."""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import PurePath
from threading import Lock
from typing import Any, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field


MAX_GIT_GATEWAY_DIAGNOSTICS = 128
# Do not evict exhausted budgets: eviction would let callers restart them.
MAX_GIT_DIAGNOSTIC_SESSIONS = 1024
MAX_GIT_DIAGNOSTIC_BYTES = 16 * 1024 * 1024
_MAX_COMMAND_CHARACTERS = 4096
_READ_SUBCOMMANDS = frozenset({
    "status", "diff", "log", "rev-list", "show", "blame", "branch",
    "grep", "ls-files", "rev-parse",
})
_EXCEPTION_KINDS = {
    ValueError: "ValueError",
    PermissionError: "PermissionError",
    TimeoutError: "TimeoutError",
    OSError: "OSError",
    FileNotFoundError: "FileNotFoundError",
    RuntimeError: "RuntimeError",
    TypeError: "TypeError",
    KeyError: "KeyError",
    subprocess.TimeoutExpired: "TimeoutExpired",
}
_RecordEvent = Callable[[Mapping[str, Any]], None]


class GitGatewayDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    attempt_id: str = Field(max_length=28, pattern=r"^att_[0-9a-f]{24}$")
    session_id: str = Field(max_length=28, pattern=r"^inv_[0-9a-f]{24}$")
    subcommand: Literal[
        "status", "diff", "log", "rev-list", "show", "blame", "branch",
        "grep", "ls-files", "rev-parse", "unknown",
    ] = "unknown"
    stage: Literal["started", "returned", "failed"]
    returncode: int | None = Field(default=None, ge=-(2**31), le=2**31 - 1)
    # UTF-8 byte counts saturate at this limit; missing/non-text output is zero.
    stdout_bytes: int = Field(default=0, ge=0, le=MAX_GIT_DIAGNOSTIC_BYTES)
    stderr_bytes: int = Field(default=0, ge=0, le=MAX_GIT_DIAGNOSTIC_BYTES)
    response_has_returncode: bool = False
    response_has_stdout: bool = False
    response_has_stderr: bool = False
    exception_kind: Literal[
        "", "ValueError", "PermissionError", "TimeoutError", "OSError",
        "FileNotFoundError", "RuntimeError", "TypeError", "KeyError",
        "TimeoutExpired", "other",
    ] = ""


@dataclass
class GitGatewayDiagnostics:
    """Reserve both event attempts before a call, including failed storage writes."""

    _counts: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    _lock: Any = field(default_factory=Lock, init=False, repr=False)

    def started(
        self, authenticated: Mapping[str, Any], command: object,
        record_event: _RecordEvent,
    ) -> GitGatewayDiagnostic | None:
        try:
            # The authenticated role is authority; profile names are not roles.
            if authenticated["assignment"].get("role") != "implementation":
                return None
            # These values come only from Manager authentication, never RPC params.
            diagnostic = GitGatewayDiagnostic(
                attempt_id=authenticated["attempt_id"],
                session_id=authenticated["assignment"]["session_id"],
                subcommand=_subcommand(command), stage="started",
            )
            with self._lock:
                count = self._counts.get(diagnostic.session_id, 0)
                if count >= MAX_GIT_GATEWAY_DIAGNOSTICS:
                    return None
                if (diagnostic.session_id not in self._counts
                        and len(self._counts) >= MAX_GIT_DIAGNOSTIC_SESSIONS):
                    return None
                self._counts[diagnostic.session_id] = count + 2
            _record(diagnostic, record_event)
            return diagnostic
        except Exception:
            return None

    def returned(
        self, diagnostic: GitGatewayDiagnostic | None, response: object,
        record_event: _RecordEvent,
    ) -> None:
        if diagnostic is None:
            return
        try:
            # The existing seam returns a plain dict. Never coerce foreign values
            # or inspect the classification (which contains the raw command).
            result = response if type(response) is dict else {}
            returncode = result.get("returncode")
            diagnostic = GitGatewayDiagnostic.model_validate({
                **diagnostic.model_dump(), "stage": "returned",
                "returncode": (returncode if type(returncode) is int
                               and -(2**31) <= returncode < 2**31 else None),
                "stdout_bytes": _byte_count(result.get("stdout")),
                "stderr_bytes": _byte_count(result.get("stderr")),
                "response_has_returncode": "returncode" in result,
                "response_has_stdout": "stdout" in result,
                "response_has_stderr": "stderr" in result,
            })
            _record(diagnostic, record_event)
        except Exception:
            pass

    def failed(
        self, diagnostic: GitGatewayDiagnostic | None, error: BaseException,
        record_event: _RecordEvent,
    ) -> None:
        if diagnostic is None:
            return
        try:
            diagnostic = GitGatewayDiagnostic.model_validate({
                **diagnostic.model_dump(), "stage": "failed",
                # Exact classes only: a user-defined subclass's name is content.
                "exception_kind": _EXCEPTION_KINDS.get(type(error), "other"),
            })
            _record(diagnostic, record_event)
        except Exception:
            pass


def _record(diagnostic: GitGatewayDiagnostic, record_event: _RecordEvent) -> None:
    try:
        record_event({
            "event_kind": "git_gateway_diagnostic",
            # Role invocation rows use the logical session, not the attempt ID.
            "invocation_id": diagnostic.session_id,
            "payload": diagnostic.model_dump(),
        })
    except Exception:
        # Diagnostic failures must not alter the operation, result or exception.
        pass


def _subcommand(command: object) -> str:
    if type(command) is not str or len(command) > _MAX_COMMAND_CHARACTERS:
        return "unknown"
    try:
        tokens = shlex.split(command)
    except ValueError:
        return "unknown"
    if tokens and PurePath(tokens[0]).name == "git":
        tokens = tokens[1:]
    if tokens and tokens[0] == "--no-pager":
        tokens = tokens[1:]
    return tokens[0] if tokens and tokens[0] in _READ_SUBCOMMANDS else "unknown"


def _byte_count(value: object) -> int:
    if type(value) is bytes:
        return min(len(value), MAX_GIT_DIAGNOSTIC_BYTES)
    if type(value) is not str:
        return 0
    if len(value) >= MAX_GIT_DIAGNOSTIC_BYTES:
        return MAX_GIT_DIAGNOSTIC_BYTES
    count = 0
    # Avoid making an output-sized temporary allocation solely for diagnostics.
    for offset in range(0, len(value), 4096):
        count += len(value[offset:offset + 4096].encode("utf-8", errors="replace"))
        if count >= MAX_GIT_DIAGNOSTIC_BYTES:
            return MAX_GIT_DIAGNOSTIC_BYTES
    return count

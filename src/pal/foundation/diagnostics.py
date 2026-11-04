"""Redacted diagnostics shared by transports and model-facing result delivery."""
from __future__ import annotations

import json
import logging
import re
import sys
import traceback
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PublicFailure(Protocol):
    public_reason: str


def exception_reason(exc: BaseException) -> str:
    """Owner-side failure presentation; complete evidence stays in service logs.

    This supplies the cause only. The operation owner supplies effect receipts
    and recovery guidance; neither can be inferred from an exception class.
    """
    logging.getLogger("pal.failure.diagnostics").warning("%s", exception_report(exc))
    if isinstance(exc, PublicFailure) and exc.public_reason.strip():
        # This text can include exact applied-item receipts, not just a cause.
        return diagnostic_text(exc.public_reason, limit=None)
    seen: set[int] = set()
    has_public_reason = False

    def causes(error: BaseException) -> list[str]:
        nonlocal has_public_reason
        if id(error) in seen:
            return []
        seen.add(id(error))
        if isinstance(error, PublicFailure) and error.public_reason.strip():
            has_public_reason = True
            return [diagnostic_text(error.public_reason, limit=None)]
        # Implicit context may be an earlier operation whose recovery failed;
        # only an explicit cause establishes that it explains this failure.
        cause = error.__cause__
        if cause is not None:
            nested = causes(cause)
            if nested:
                return nested
        if isinstance(error, BaseExceptionGroup):
            return [item for child in error.exceptions for item in causes(child)]
        message = diagnostic_reason(str(error))
        return [f"{type(error).__name__}: {message}" if message else type(error).__name__]

    text = "; ".join(dict.fromkeys(causes(exc)))
    return diagnostic_text(text, limit=None) if has_public_reason else diagnostic_reason(text)


def diagnostic_reason(value: object, *, limit: int | None = 600) -> str:
    """Render an external failure at its owner boundary, keeping evidence in logs."""
    text = (json.dumps(diagnostic_value(value), ensure_ascii=False)
            if isinstance(value, (Mapping, list, tuple)) else str(value)).strip()
    summary = text
    if text.startswith(("{", "[")):
        try:
            decoded = json.loads(text)
        except (ValueError, RecursionError):
            pass
        else:
            # Encoded diagnostic records need projection before JSON escapes
            # hide their frames. Successful business strings never enter here.
            summary = json.dumps(public_diagnostics(decoded, _diagnostic=True),
                                 ensure_ascii=False, separators=(",", ":"))
    summary = diagnostic_summary(summary)
    # MCP and sidecars may return JavaScript/Java stack frames rather than
    # Python tracebacks. Apply this only to owner-selected failure text.
    summary = re.sub(r"(?m)^[ \t]+at [^\n]*(?:\([^\n]*\)|:\d+|<anonymous>)[^\n]*\n?", "", summary).strip()
    if summary != text:
        logging.getLogger("pal.failure.diagnostics").warning("%s", diagnostic_text(text, limit=None))
        # Python tracebacks present the original cause before its wrappers.
        causes = re.findall(r"(?m)^[\w.]*(?:Error|Exception|Failure):[^\n]*", summary)
        java_causes = re.findall(r"(?m)^\s*Caused by:\s*(.+)$", summary)
        if "ExceptionGroup:" in summary:
            # Each group branch may have its own cause chain. Keep one root
            # per branch instead of dropping every failure after the first.
            roots = []
            for branch in re.split(r"(?m)^ *\+[-+ 0-9]*$", text):
                branch = re.sub(r"(?m)^ *\| ?", "", branch)
                messages = re.findall(r"(?m)^[\w.]*(?:Error|Exception|Failure):[^\n]*", branch)
                if messages:
                    roots.append(messages[0])
            if roots:
                summary = "; ".join(dict.fromkeys(roots))
        elif java_causes:
            summary = java_causes[-1]
        elif causes:
            summary = causes[0]
    summary = diagnostic_text(summary, limit=None)
    if limit is None or len(summary) <= limit:
        return summary
    if summary == text:
        logging.getLogger("pal.failure.diagnostics").warning("%s", summary)
    marker = " … [details in service logs] … "
    available = max(0, limit - len(marker))
    head = (available + 1) // 2
    tail = available // 2
    return summary[:head] + marker + (summary[-tail:] if tail else "")


def public_diagnostics(value: Any, *, _diagnostic: bool = False) -> Any:
    """Project owner diagnostic records without changing the retained evidence.

    Owners call this for health/failure records, never ordinary tool business
    output (for example a user's source file or a command's stdout).
    """
    if isinstance(value, Mapping):
        projected = {key: public_diagnostics(item, _diagnostic=_diagnostic or bool(re.search(
                    r"error|diagnostic|failure|reason", str(key), re.IGNORECASE)))
                for key, item in diagnostic_value(value).items()
                if re.sub(r"[^a-z]", "", str(key).lower()) not in {
                    "traceback", "stack", "stacktrace", "errordiagnostic", "failureattempts"}}
        if _diagnostic and not any(str(key).lower() in {"message", "error", "reason", "cause", "detail"}
                                   and item for key, item in projected.items()):
            roots = [diagnostic_reason(item) for key, item in value.items()
                     if re.sub(r"[^a-z]", "", str(key).lower()) in {"traceback", "stack", "stacktrace"}
                     and isinstance(item, str) and item.strip()]
            if roots:
                projected["reason"] = "; ".join(dict.fromkeys(roots))
        return projected
    if isinstance(value, (list, tuple)):
        return [public_diagnostics(item, _diagnostic=_diagnostic) for item in value]
    if isinstance(value, str):
        return diagnostic_reason(value) if _diagnostic else value
    return value


def diagnostic_text(value: object, *, limit: int | None = 2000, tail: bool = False) -> str:
    text = str(value)
    if "\\" in text:
        # Providers and validation errors can embed JSON in otherwise plain
        # diagnostics. Decode escaped string values before matching credentials:
        # a literal \\n is not whitespace and would swallow the next cause.
        def redact_escaped_string(match: re.Match[str]) -> str:
            encoded = match.group()
            if "\\" not in encoded:
                return encoded
            try:
                decoded = json.loads(encoded)
            except ValueError:
                return encoded
            redacted = diagnostic_text(decoded, limit=None)
            return encoded if redacted == decoded else json.dumps(redacted, ensure_ascii=False)

        text = re.sub(r'(?<!\\)"(?:[^"\\]|\\.)*"', redact_escaped_string, text)
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", text)
    text = re.sub(
        r'''(?i)(["']?(?:password|passwd|(?:access|refresh|auth)[_-]?token|token|api[_-]?key|secret)["']?\s*[=:]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\[redacted\]|[^\s,;}\]]+)''',
        r"\1[redacted]", text,
    )
    # Start only at a scheme boundary. Retrying the greedy scheme at every
    # character makes long ordinary error messages quadratic to redact.
    text = re.sub(r"(?<![a-zA-Z0-9+.-])([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", text)
    if limit is None or len(text) <= limit:
        return text
    return "[diagnostic tail] … " + text[-limit:] if tail else text[:limit] + " … [diagnostic truncated]"


def exception_diagnostic(exc: BaseException) -> str:
    parts = []
    seen = set()
    while exc is not None and id(exc) not in seen and len(parts) < 3:
        seen.add(id(exc))
        parts.append(f"{type(exc).__name__}: {exc}")
        exc = exc.__cause__
    return diagnostic_text("; caused by ".join(parts))


def diagnostic_value(value: Any, *, summarize: bool = False) -> Any:
    """Redact before JSON escaping so secrets and following evidence stay distinct."""
    if isinstance(value, Mapping):
        return {
            key: "[redacted]" if re.fullmatch(
                r"password|passwd|(?:access|refresh|auth)[_-]?token|token|api[_-]?key|secret|(?:proxy[-_]?)?authorization",
                str(key), re.IGNORECASE,
            ) else diagnostic_value(item, summarize=summarize)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [diagnostic_value(item, summarize=summarize) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return diagnostic_summary(str(value)) if summarize else diagnostic_text(value, limit=None)


def exception_report(exc: BaseException) -> str:
    """Preserve the redacted traceback and chain for internal logs and records."""
    report = traceback.TracebackException.from_exception(
        exc, max_group_width=sys.maxsize, max_group_depth=sys.maxsize,
    )
    return diagnostic_text("".join(report.format()).rstrip(), limit=None)


def exception_summary(exc: BaseException) -> str:
    """Show exception messages and causes without repeating stack frames."""
    seen: set[int] = set()

    def render(error: BaseException) -> list[str]:
        if id(error) in seen:
            return []
        seen.add(id(error))
        lines = ["".join(traceback.format_exception_only(type(error), error)).rstrip()]
        cause = error.__cause__ or (error.__context__ if not error.__suppress_context__ else None)
        if cause is not None:
            lines.extend("Caused by: " + line for line in render(cause))
        if isinstance(error, BaseExceptionGroup):
            for child in error.exceptions:
                lines.extend(render(child))
        return lines

    return diagnostic_text("\n".join(render(exc)), limit=None)


def diagnostic_summary(value: str) -> str:
    """Remove standard traceback frames, retaining messages from every cause.

    Used only for a preview whose original report is retained separately.
    """
    lines = []
    in_traceback = False
    in_frame = False
    group_traceback = any(re.fullmatch(r" *\+-[-+ 0-9]*", line) for line in value.splitlines())
    for line in value.splitlines():
        # ExceptionGroup tracebacks indent each branch behind a tree gutter.
        # Remove that presentation before applying the ordinary frame rules.
        if group_traceback:
            line = re.sub(r"^ *\| ?", "", line)
        if group_traceback and re.fullmatch(r" *\+-[-+ 0-9]*", line):
            continue
        if line.strip() == "+ Exception Group Traceback (most recent call last):":
            in_traceback = True
            in_frame = False
            continue
        # Owners may prefix a retained report (for example "text: " or a
        # post-commit cleanup label). The following frames are still private.
        if line.rstrip().endswith("Traceback (most recent call last):"):
            in_traceback = True
            in_frame = False
            continue
        if in_traceback and line.startswith('  File "'):
            in_frame = True
            continue
        if in_frame and line.startswith("    "):
            continue
        in_frame = False
        if line in {"The above exception was the direct cause of the following exception:",
                    "During handling of the above exception, another exception occurred:"}:
            continue
        lines.append(line)
    return diagnostic_text("\n".join(lines).strip(), limit=None)

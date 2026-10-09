"""Redacted diagnostics shared by transports and model-facing result delivery."""
from __future__ import annotations

import json
import re
import sys
import traceback
from collections.abc import Mapping
from typing import Any


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
                r"password|passwd|(?:access|refresh|auth)[_-]?token|token|api[_-]?key|secret",
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
    """Preserve the complete traceback and exception chain for tool delivery.

    Redaction is explicit. Output budgeting belongs to the delivery layer,
    which can retain a complete snapshot rather than discard diagnostic facts.
    """
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
        if line == "Traceback (most recent call last):":
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

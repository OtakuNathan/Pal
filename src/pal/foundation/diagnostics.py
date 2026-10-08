"""Redacted diagnostics shared by transports and model-facing result delivery."""
from __future__ import annotations

import re
import sys
import traceback


def diagnostic_text(value: object, *, limit: int | None = 2000, tail: bool = False) -> str:
    text = str(value)
    text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", text)
    text = re.sub(
        r'''(?i)(["']?(?:password|passwd|(?:access|refresh|auth)[_-]?token|token|api[_-]?key|secret)["']?\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;}\]]+)''',
        r"\1[redacted]", text,
    )
    text = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]+:[^/@\s]+@", r"\1[redacted]@", text)
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


def exception_report(exc: BaseException) -> str:
    """Preserve the complete traceback and exception chain for tool delivery.

    Redaction is explicit. Output budgeting belongs to the delivery layer,
    which can retain a complete snapshot rather than discard diagnostic facts.
    """
    report = traceback.TracebackException.from_exception(
        exc, max_group_width=sys.maxsize, max_group_depth=sys.maxsize,
    )
    return diagnostic_text("".join(report.format()).rstrip(), limit=None)

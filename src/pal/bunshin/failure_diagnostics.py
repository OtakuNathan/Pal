"""Bounded worker failure metadata, without source text or runtime values."""
from __future__ import annotations

from collections import deque
import json
import re
from typing import Any

_MAX_FRAMES = 12


def _label(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^a-zA-Z0-9_.<>-]", "_", value[:limit])


def exception_diagnostic(exc: BaseException) -> dict[str, Any]:
    # Read code metadata directly: traceback formatting can load source lines.
    frames: deque[dict[str, Any]] = deque(maxlen=_MAX_FRAMES)
    traceback = exc.__traceback__
    while traceback is not None:
        code = traceback.tb_frame.f_code
        frames.append({
            "file": _label(code.co_filename.replace("\\", "/").rsplit("/", 1)[-1], 96),
            "function": _label(code.co_name, 96),
            "line": traceback.tb_lineno,
        })
        traceback = traceback.tb_next
    return {"error_type": _label(type(exc).__name__, 96), "frames": list(frames)}


def append_failure_diagnostic(error: str, diagnostic: object) -> str:
    """Carry optional metadata through existing durable error-text contracts.

    Old workers omit it. Validate the additive wire field so arbitrary nested
    values, paths or unbounded lists cannot get copied into failure artifacts.
    """
    if not isinstance(diagnostic, dict):
        return error
    error_type = _label(diagnostic.get("error_type"), 96)
    if not error_type:
        return error
    frames = []
    candidates = diagnostic.get("frames")
    for frame in candidates[-_MAX_FRAMES:] if isinstance(candidates, list) else []:
        if not isinstance(frame, dict):
            continue
        filename = frame.get("file")
        line = frame.get("line")
        if not isinstance(filename, str) or type(line) is not int or not 0 < line < 10**9:
            continue
        frames.append({
            "file": _label(filename.replace("\\", "/").rsplit("/", 1)[-1], 96),
            "function": _label(frame.get("function"), 96),
            "line": line,
        })
    summary = json.dumps({"error_type": error_type, "frames": frames}, separators=(",", ":"))
    return f"{error}\nworker_diagnostic={summary}"

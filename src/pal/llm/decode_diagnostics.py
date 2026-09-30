"""Bounded, private failure captures of the transport-to-codec boundary.

Response text and tool arguments are retained for reproduction. Requests,
credentials and HTTP headers are never captured. These are decoded SDK frames,
not raw HTTP/SSE bytes. Successful attempts never write a capture.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pal.foundation.log_paths import pal_log_root
from pal.shared.json_values import thaw_json

logger = logging.getLogger(__name__)
_WRITE_LOCK = threading.Lock()
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_CAPTURES = 20


class DecodeFailureCapture:
    def __init__(self, *, max_bytes: int = MAX_CAPTURE_BYTES) -> None:
        self.max_bytes = max(1024, max_bytes)
        self.frames: deque[bytes] = deque()
        self.retained_bytes = 0
        self.total_frames = 0
        self.dropped_frames = 0
        self.truncated_frames = 0
        self.capture_errors = 0

    def observe(self, frame: Any) -> None:
        self.total_frames += 1
        try:
            self._observe(frame)
        except Exception:
            # Diagnostics must not turn an otherwise usable response into a failure.
            self.capture_errors += 1

    def _observe(self, frame: Any) -> None:
        encoded = json.dumps({
            "sequence": frame.sequence, "payload": thaw_json(frame.payload),
        }, ensure_ascii=False).encode("utf-8")
        if len(encoded) > self.max_bytes:
            # Preserve the tail of an oversized event, explicitly non-replayable.
            self.truncated_frames += 1
            encoded = json.dumps({
                "sequence": frame.sequence, "truncated": True,
                "original_bytes": len(encoded),
                "payload_tail": encoded[-self.max_bytes // 8:].decode("utf-8", errors="replace"),
            }, ensure_ascii=False).encode("utf-8")
        while self.frames and self.retained_bytes + len(encoded) > self.max_bytes:
            self.retained_bytes -= len(self.frames.popleft())
            self.dropped_frames += 1
        self.frames.append(encoded)
        self.retained_bytes += len(encoded)

    def save(self, exc: BaseException, **metadata: Any) -> Path | None:
        """Best effort: persistence must never replace the original failure."""
        temporary: str | None = None
        try:
            root = pal_log_root() / "llm-decode-failures"
            document = {
                "version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "boundary": "transport_to_codec_json_frames",
                **metadata,
                "error_type": type(exc).__name__,
                "error": str(exc)[:4096],
                "traceback": "".join(traceback.TracebackException.from_exception(
                    exc, capture_locals=False,
                ).format())[-32768:],
                "total_frames": self.total_frames,
                "dropped_frames": self.dropped_frames,
                "truncated_frames": self.truncated_frames,
                "capture_errors": self.capture_errors,
                "complete_capture": not (self.dropped_frames or self.truncated_frames or self.capture_errors),
                "frames": [json.loads(frame) for frame in self.frames],
            }
            with _WRITE_LOCK:
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                root.chmod(0o700)
                fd, temporary = tempfile.mkstemp(prefix="decode-", suffix=".tmp", dir=root)
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(document, output, ensure_ascii=False)
                target = Path(temporary).with_suffix(".json")
                os.replace(temporary, target)
                temporary = None
                captures = sorted(root.glob("decode-*.json"), key=lambda p: p.stat().st_mtime_ns)
                for expired in captures[:-MAX_CAPTURES]:
                    expired.unlink(missing_ok=True)
            return target
        except Exception as capture_error:
            logger.warning("Could not save LLM decode capture: %s", type(capture_error).__name__)
            return None
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except OSError:
                    pass

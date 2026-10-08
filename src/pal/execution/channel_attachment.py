from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EffectOutcome, EffectReceipt
from pal.execution.turn_io_contracts import TurnIOHost
from pal.foundation import AttachmentSpec
from pal.shared import RuntimeStatus


@dataclass
class ChannelSendAttachmentTool:
    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return _failure(RuntimeStatus.INVALID, "async_required", "this tool requires async turn context")

    async def ainvoke(self, args: dict[str, Any], *, runtime: TurnIOHost | None = None, turn_id: str | None = None) -> CapabilityResult:
        if not str(turn_id or "").strip():
            return _failure(RuntimeStatus.INVALID, "turn_id_required", "turn_id is required")
        turn_io = runtime.turn_io if runtime is not None else None
        if turn_io is None:
            return _failure(RuntimeStatus.UNSUPPORTED, "core_turn_io_missing", "core turn I/O port is not available")
        path_text = str(args.get("path") or "").strip()
        if not path_text:
            return _failure(RuntimeStatus.INVALID, "path_required", "path is required")
        path = Path(path_text).expanduser()
        if not path.is_file():
            return _failure(RuntimeStatus.NOT_FOUND, "file_not_found", f"file not found: {path}", path=str(path))
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        file_name = str(args.get("file_name") or "").strip() or resolved.name
        mime_type = str(args.get("mime_type") or "").strip() or (mimetypes.guess_type(str(resolved))[0] or "")
        attachment = AttachmentSpec(
            path=str(resolved),
            caption=str(args.get("caption") or ""),
            file_name=file_name,
            mime_type=mime_type,
        )
        result = await turn_io.send_attachment_for_turn(turn_id, attachment)
        if isinstance(result, CapabilityResult):
            return result
        return _failure(RuntimeStatus.ERROR, "invalid_core_turn_io_result", "core turn I/O returned an invalid result", not_started=False)


def _failure(status: str, reason: str, text: str, *, not_started: bool = True, **structured: Any) -> CapabilityResult:
    payload = {"reason": reason, "error_code": reason, "kind": "rejected" if not_started else "failed",
               "retry": "correct_input" if not_started else "reconcile_first", **structured}
    return CapabilityResult(
        status=status,
        text=text,
        llm_text=f"Could not send attachment: {text}.",
        structured=payload,
        effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED if not_started else EffectOutcome.UNKNOWN),
    )

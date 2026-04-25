from __future__ import annotations

import inspect
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.foundation import AttachmentSpec
from pal.shared import RuntimeStatus


@dataclass
class ChannelSendAttachmentTool:
    name: str = "op_channel_send_attachment"
    display_name: str = "Send Attachment"
    family: str = "channel"
    description: str = "Send a local file attachment back to the channel that started the current turn."
    tags: tuple[str, ...] = ("channel", "attachment", "file", "send")
    keywords: tuple[str, ...] = ("send", "file", "attachment", "telegram", "document")
    args_schema: dict[str, object] = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Local filesystem path to the file to send."},
            "caption": {"type": "string", "description": "Optional caption to send with the attachment."},
            "file_name": {"type": "string", "description": "Optional display filename."},
            "mime_type": {"type": "string", "description": "Optional MIME type hint."},
        },
        "required": ["path"],
    })
    result_schema: dict[str, object] = field(default_factory=lambda: {
        "type": "object",
        "properties": {
            "attachment_id": {"type": "string"},
            "path": {"type": "string"},
            "file_name": {"type": "string"},
            "mime_type": {"type": "string"},
            "reason": {"type": "string"},
        },
    })

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return CapabilityResult(
            status=RuntimeStatus.INVALID,
            text="op_channel_send_attachment requires async turn context",
            llm_text="Could not send attachment: this tool requires async turn context.",
            structured={"reason": "async_required"},
        )

    async def ainvoke(self, args: dict[str, Any], *, runtime: Any = None, turn_id: str | None = None) -> CapabilityResult:
        if not str(turn_id or "").strip():
            return _failure(RuntimeStatus.INVALID, "turn_id_required", "turn_id is required")
        runtime_registry = getattr(runtime, "provider_registry", {}) if runtime is not None else {}
        turn_io = runtime_registry.get("core:turn_io") if isinstance(runtime_registry, dict) else None
        send = getattr(turn_io, "send_attachment_for_turn", None)
        if not callable(send):
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
        result = send(turn_id, attachment)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, CapabilityResult):
            return result
        return _failure(RuntimeStatus.ERROR, "invalid_core_turn_io_result", "core turn I/O returned an invalid result")


def _failure(status: str, reason: str, text: str, **structured: Any) -> CapabilityResult:
    payload = {"reason": reason, **structured}
    return CapabilityResult(
        status=status,
        text=text,
        llm_text=f"Could not send attachment: {text}.",
        structured=payload,
    )

"""Read-only projection of executed tool results for review evidence."""
from __future__ import annotations
import hashlib
import json
from typing import Any
from uuid import uuid4
from pal.shared import ToolExecutionResult
from pal.shared.tool_protocol import ToolCallIR


def _review_tool_evidence_ref(
    target_name: str,
    tool_call: ToolCallIR,
    result: ToolExecutionResult,
) -> dict[str, Any]:
    if not (
        str(target_name).startswith(("op_exec_shell", "op_lsp_"))
        or str(target_name) in {
            "op_file_write",
            "op_file_edit",
            "op_bunshin_verification_scratch_write",
        }
    ):
        return {}
    output_text = str(result.text or result.llm_text or "")
    structured = json.loads(
        json.dumps(dict(result.structured or {}), ensure_ascii=False, default=str)
    )
    effective_args = dict(tool_call.args or {})
    if tool_call.name == "op_tool_call" and isinstance(effective_args.get("args"), dict):
        effective_args = dict(effective_args["args"])
    args = json.loads(json.dumps(effective_args, ensure_ascii=False, default=str))
    encoded = json.dumps(
        {"text": output_text, "structured": structured},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return {
        "evidence_ref_id": f"tev_{uuid4().hex[:12]}",
        "kind": (
            "test_write"
            if str(target_name) in {
                "op_file_write",
                "op_file_edit",
                "op_bunshin_verification_scratch_write",
            }
            else "lsp"
            if str(target_name).startswith("op_lsp_")
            else "command"
        ),
        "tool_name": str(target_name),
        "call_id": str(tool_call.call_id or ""),
        "ok": bool(result.ok),
        "status": str(result.status or ""),
        "args": args,
        "summary": output_text[:500],
        "output_sha256": hashlib.sha256(encoded).hexdigest(),
        "output_text": output_text[:65536],
        "structured": structured,
    }

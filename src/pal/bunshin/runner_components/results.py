from __future__ import annotations
from pal.bunshin.runner_components.result_values import _extract_ask_user_question_payload
from pal.bunshin.runner_components.result_values import _extract_lessons_and_clean_summary
from pal.bunshin.runner_components.result_values import _compact_preview_text
from dataclasses import dataclass
from typing import Any
from pal.bunshin.user_interaction import ask_user_question_summary as _ask_user_question_summary
from pal.bunshin.runner_components.artifacts import Artifacts
from pal.bunshin.runner_components.memory_results import MemoryResults
from pal.bunshin.runner_components.status import Status
from pal.foundation.diagnostics import diagnostic_text


@dataclass
class Results:
    artifacts: Artifacts
    memory_results: MemoryResults
    status: Status

    def terminal_payload(self, status: str, summary: Any) -> dict[str, Any]:
        resolved_status = str(status or "").strip() or "completed"
        summary_text = str(summary or "").strip()
        ask_user_question = _extract_ask_user_question_payload(summary_text)
        lesson_payload = _extract_lessons_and_clean_summary(summary_text)
        full_summary = str(lesson_payload.get("summary") or summary_text).strip()
        summary_text = self.short_summary(full_summary)
        experience_payload = {
            "task_lessons": list(lesson_payload.get("task_lessons") or []),
            "system_lessons": list(lesson_payload.get("system_lessons") or []),
            "memory_candidates": [dict(item) for item in self.memory_results.memory_candidates],
        }
        if self.memory_results.memory_generation_id:
            experience_payload["memory_generation_id"] = self.memory_results.memory_generation_id
            entries = self.memory_results.result_memory_service.l2_entries() if self.memory_results.result_memory_service is not None else ()
            experience_payload["memory_refs"] = sorted({entry.source_ref for entry in entries
                if entry.source_ref.startswith(("fact:", "case:"))})
        payload = {
            "status": resolved_status,
            "summary": summary_text,
            **experience_payload,
            **self.artifacts.artifact_payload(),
        }
        if full_summary != summary_text:
            payload["details"] = diagnostic_text(full_summary, limit=None)
        if self.status.diagnostics:
            payload["diagnostics"] = list(self.status.diagnostics)
        if self.status.blocked_kind:
            payload["blocker_kind"] = self.status.blocked_kind
        if ask_user_question:
            payload["status"] = "blocked"
            payload["summary"] = _ask_user_question_summary(ask_user_question)
            payload["ask_user_question"] = ask_user_question
        return payload

    def cancel_terminal_payload(self, cancel: dict[str, Any]) -> dict[str, Any]:
        payload = self.terminal_payload("killed", cancel.get("summary") or cancel.get("reason") or "bunshin cancellation requested")
        payload["reason"] = str(cancel.get("reason") or "cooperative_cancel_requested")
        payload["cooperative_cancel"] = True
        for key in ("workflow_id", "invocation_id", "unit_id", "repair_bill_ref"):
            value = str(cancel.get(key) or "").strip()
            if value:
                payload[key] = value
        return payload

    def restart_terminal_payload(self, restart: dict[str, Any]) -> dict[str, Any]:
        payload = self.terminal_payload(
            "suspended",
            restart.get("summary") or "bunshin suspended for manager restart",
        )
        payload["reason"] = str(restart.get("reason") or "manager_restart_requested")
        payload["manager_restart"] = True
        payload["durable_safe_point"] = True
        return payload

    @staticmethod
    def short_summary(value: Any, *, limit: int = 500) -> str:
        text = _compact_preview_text(str(value or ""))
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

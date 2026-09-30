from __future__ import annotations
from pal.bunshin.runner_components.models import BunshinAgentLoopState
from pal.bunshin.runner_components.models import EventWriter
from pal.bunshin.runner_components.llm_settings import _prompt_observation_tag_from_pack
from pal.bunshin.runner_components.progress_text import _progress_summary
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from pal.core.prompt_debug_log import append_prompt_debug_log, render_llm_outcome_debug_log, render_prompt_debug_log, render_reply_debug_log
from pal.foundation import utc_now
from pal.llm.ir import LLMRequestIR
from pal.shared import BunshinInvocationPack


@dataclass
class Reporter:
    bunshin_id: str
    pack: BunshinInvocationPack
    run_id: str
    write_event: EventWriter

    def debug_log_bunshin_llm_request(self, state: BunshinAgentLoopState, request: LLMRequestIR) -> None:
        context = self.prompt_debug_context(round=state.llm_round_count)
        self.append_prompt_debug_log(render_prompt_debug_log(request, context=context))

    def debug_log_bunshin_llm_outcome(self, state: BunshinAgentLoopState, outcome: Any) -> None:
        context = self.prompt_debug_context(round=state.llm_round_count)
        self.append_prompt_debug_log(
            render_llm_outcome_debug_log(
                outcome,
                provider_payload="{}",
                context=context,
            )
        )

    def debug_log_bunshin_reply(self, text: object) -> None:
        self.append_prompt_debug_log(render_reply_debug_log(text, context=self.prompt_debug_context()))

    async def emit(self, event_kind: str, payload: dict[str, Any]) -> None:
        binding = dict(dict(self.pack.metadata or {}).get("bunshin_v2") or {})
        event_payload = dict(payload)
        event = {
            "type": "event",
            "event_kind": event_kind,
            "bunshin_id": self.bunshin_id,
            "run_id": self.run_id,
            "invocation_id": self.pack.invocation_id,
            "workflow_id": str(binding.get("workflow_id") or ""),
            "bunshin_profile": self.pack.bunshin_profile,
            "payload": event_payload,
            "created_at": utc_now(),
        }
        self.append_debug_log("runner_event", event)
        await self.write_event(event)

    async def emit_progress(self, phase: str, **payload: Any) -> None:
        await self.emit(
            "progress",
            {
                "phase": phase,
                "summary": _progress_summary(phase, payload),
                **payload,
            },
        )

    def append_debug_log(self, section: str, payload: dict[str, Any]) -> None:
        config = dict((self.pack.metadata or {}).get("debug_log") or {})
        if not bool(config.get("enabled")):
            return
        path_text = str(config.get("path") or "").strip()
        if not path_text:
            return
        record = {
            "created_at": utc_now(),
            "section": str(section or "debug"),
            "invocation_id": self.pack.invocation_id,
            "bunshin_profile": self.pack.bunshin_profile,
            "bunshin_id": self.bunshin_id,
            "run_id": self.run_id,
            "payload": dict(payload or {}),
        }
        prompt_observation_tag = _prompt_observation_tag_from_pack(self.pack)
        if prompt_observation_tag:
            record["prompt_observation_tag"] = prompt_observation_tag
        try:
            path = Path(path_text)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        except Exception:
            return

    def append_prompt_debug_log(self, text: str) -> None:
        config = dict((self.pack.metadata or {}).get("debug_log") or {})
        if not bool(config.get("enabled")):
            return
        path_text = str(config.get("path") or "").strip()
        if not path_text:
            return
        try:
            append_prompt_debug_log(Path(path_text), text)
        except Exception:
            return

    def prompt_debug_context(self, *, round: int | None = None) -> dict[str, Any]:
        context: dict[str, Any] = {
            "invocation_id": self.pack.invocation_id,
            "bunshin_profile": self.pack.bunshin_profile,
            "bunshin_id": self.bunshin_id,
            "run_id": self.run_id,
        }
        if round is not None:
            context["round"] = round
        prompt_observation_tag = _prompt_observation_tag_from_pack(self.pack)
        if prompt_observation_tag:
            context["prompt_observation_tag"] = prompt_observation_tag
        return context

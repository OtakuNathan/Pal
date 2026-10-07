from __future__ import annotations
from pal.bunshin.runner_components.models import BunshinRuntimeBundle
import asyncio
from dataclasses import dataclass
from pal.bunshin.runner_components.agent_session import AgentSession
from pal.bunshin.runner_components.artifacts import Artifacts
from pal.bunshin.runner_components.completion import Completion
from pal.bunshin.runner_components.control import Control
from pal.bunshin.runner_components.reporter import Reporter
from pal.bunshin.runner_components.results import Results
from pal.bunshin.runner_components.status import Status
from pal.bunshin.runner_components.text_deliverables import TextDeliverables


@dataclass
class Invocation:
    agent_session: AgentSession
    artifacts: Artifacts
    completion: Completion
    control: Control
    reporter: Reporter
    results: Results
    status: Status
    text_deliverables: TextDeliverables

    @staticmethod
    async def close_execution_work(bundle):
        close = getattr(getattr(bundle, "execution_runtime", None), "close_role_work", None)
        if close is not None:
            await close()

    async def run_invocation(
        self,
        bundle: BunshinRuntimeBundle,
        *,
        prompt_observation_tag: str = "",
    ) -> int:
        payload = {
            "phase": "invocation_started",
            "summary": "Bunshin V2 worker invocation started",
        }
        if prompt_observation_tag:
            payload["prompt_observation_tag"] = prompt_observation_tag
        await self.reporter.emit("phase_started", payload)
        retry_note = ""
        while True:
            await self.control.raise_if_cancel_requested()
            await self.control.raise_if_restart_requested()
            progress_before = await asyncio.to_thread(self.completion.completion_gate_progress_marker)
            final_text = await self.agent_session.run_agent_loop(bundle, forced_retry_note=retry_note)
            await self.control.raise_if_cancel_requested()
            await self.control.raise_if_restart_requested()
            if self.status.blocked_summary:
                await self.close_execution_work(bundle)
                await self.reporter.emit("terminal", self.results.terminal_payload("blocked", self.status.blocked_summary))
                return 0
            if not self.completion.required_primary_artifact_name() or self.completion.completion_evidence_present():
                break
            progress_after = await asyncio.to_thread(self.completion.completion_gate_progress_marker)
            if retry_note and progress_after == progress_before:
                self.status.classify_blocker("completion_gate_stalled")
                self.status.block("completion gate stalled: the required primary artifact is still absent after explicit "
                    "submit feedback, and the worker made no checklist, finding, or artifact content progress")
                await self.reporter.emit_progress(
                    "completion_gate_stalled",
                    round=0,
                    summary=self.status.blocked_summary,
                )
                await self.reporter.emit(
                    "terminal",
                    self.results.terminal_payload("blocked", self.status.blocked_summary),
                )
                return 0
            retry_note = self.completion.missing_completion_evidence_feedback()
            await self.reporter.emit_progress(
                "completion_gate_rejected",
                round=0,
                summary=retry_note,
            )
        await self.text_deliverables.persist_text_deliverable_if_needed(final_text)
        self.artifacts.finalize_produced_artifacts()
        await self.reporter.emit(
            "terminal",
            self.results.terminal_payload("completed", final_text or "Bunshin V2 invocation completed"),
        )
        return 0

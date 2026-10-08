from __future__ import annotations
from pal.bunshin.failure_diagnostics import exception_diagnostic
from pal.bunshin.runner_components.models import BunshinRuntimeBundle as BunshinRuntimeBundle
from pal.bunshin.runner_components.models import _BunshinCooperativeCancel as _BunshinCooperativeCancel
from pal.bunshin.runner_components.models import _BunshinCooperativeRestart as _BunshinCooperativeRestart
from pal.bunshin.runner_components.models import EventWriter as EventWriter
from pal.bunshin.runner_components.models import DecisionReader as DecisionReader
from pal.bunshin.runner_components.runtime_build import build_slim_bunshin_runtime as build_slim_bunshin_runtime
from pal.bunshin.runner_components.llm_settings import _prompt_observation_tag_from_pack as _prompt_observation_tag_from_pack
import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from pal.bunshin.checkpoint import AgentSessionCheckpointError
from pal.bunshin.prompt_adapter import prompt_scaffold_summary as _prompt_scaffold_summary
from pal.shared import BunshinInvocationPack
from pal.bunshin.runner_components.composition import RunnerComponents, build_runner_components
from pal.bunshin.runner_components.models import BunshinLLMRetryableError as BunshinLLMRetryableError


@dataclass
class BunshinRunner:
    runtime_root: Path
    pack: BunshinInvocationPack
    bunshin_id: str
    run_id: str
    write_event: EventWriter
    read_decision: DecisionReader
    runtime_bundle: BunshinRuntimeBundle | None = None
    components: RunnerComponents = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.components = build_runner_components(
            runtime_root=self.runtime_root, pack=self.pack, bunshin_id=self.bunshin_id,
            run_id=self.run_id, write_event=self.write_event, read_decision=self.read_decision,
        )

    async def run(self) -> int:
        bundle: BunshinRuntimeBundle | None = None
        prompt_observation_tag = _prompt_observation_tag_from_pack(self.pack)
        runner_started_payload = {
            "goal": self.pack.goal,
            "instruction": self.pack.instruction,
            "allowed_capabilities": list(self.pack.allowed_capabilities),
        }
        if prompt_observation_tag:
            runner_started_payload["prompt_observation_tag"] = prompt_observation_tag
        self.components.reporter.append_debug_log("runner_started", runner_started_payload)
        try:
            bundle = self.runtime_bundle or build_slim_bunshin_runtime(
                self.runtime_root,
                run_id=self.run_id,
                llm_authority="manager_proxy",
                snapshot_root=Path(self.pack.workspace["run_dir"]) if self.pack.workspace.get("run_dir") else None,
                memory_workflow_id=str((self.pack.workspace.get("bunshin_v2") or {}).get("workflow_id")
                    or (self.pack.metadata.get("bunshin_v2") or {}).get("workflow_id") or ""),
            )
            self.components.memory_results.bind_generation(bundle.memory_generation_id, bundle.memory_service)
            accepted_payload = {
                "phase": "accepted",
                "summary": "bunshin accepted task context",
                "prompt_scaffold_summary": _prompt_scaffold_summary(self.components.prompt_context.prompt_scaffold()),
            }
            if prompt_observation_tag:
                accepted_payload["prompt_observation_tag"] = prompt_observation_tag
            await self.components.reporter.emit(
                "phase_started",
                accepted_payload,
            )
            return await self.components.invocation.run_invocation(bundle, prompt_observation_tag=prompt_observation_tag)
        except _BunshinCooperativeCancel as cancel:
            await self._report_terminal_after_cleanup(bundle, self.components.results.cancel_terminal_payload(cancel.payload))
            return 0
        except _BunshinCooperativeRestart as restart:
            await self._report_terminal_after_cleanup(bundle, self.components.results.restart_terminal_payload(restart.payload))
            return 0
        except Exception as exc:
            checkpoint_error = isinstance(exc, AgentSessionCheckpointError)
            await self._report_terminal_after_cleanup(
                bundle,
                {
                    "status": "failed",
                    "summary": f"bunshin runner failed: {exc.__class__.__name__}",
                    "error": str(exc),
                    "failure_diagnostic": exception_diagnostic(exc),
                    "error_type": exc.__class__.__name__,
                    "error_kind": (
                        "invalid_agent_session_checkpoint"
                        if checkpoint_error
                        else "runner_failure"
                    ),
                    "retry_directive": (
                        "do_not_retry" if checkpoint_error else "reconcile_first"
                    ),
                    "task_lessons": [],
                    "system_lessons": [],
                },
            )
            return 1
        finally:
            if bundle is not None:
                await bundle.close()
            self.components.reporter.append_debug_log("runner_stopped", {"blocked_summary": self.components.status.blocked_summary})

    async def _report_terminal_after_cleanup(
        self, bundle: BunshinRuntimeBundle | None, payload: dict[str, Any],
    ) -> None:
        try:
            await self.components.invocation.close_execution_work(bundle)
        except Exception as exc:
            # Preserve the primary failure's classification and retry policy.
            # Cleanup still fails the process, but cannot suppress its terminal.
            payload["cleanup_error"] = {
                "error": str(exc), "error_type": type(exc).__name__,
                "failure_diagnostic": exception_diagnostic(exc),
            }
            raise
        finally:
            with contextlib.suppress(Exception):
                await self.components.reporter.emit("terminal", payload)

from __future__ import annotations
from pal.bunshin.runner_components.models import BunshinRuntimeBundle
from pal.bunshin.runner_components.models import BunshinAgentLoopState
from pal.bunshin.runner_components.result_values import _memory_candidates_from_sink
from pal.bunshin.runner_components.result_values import _fsync_directory
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4
from pal.core.runtime_state import RuntimeSnapshotIdentity, runtime_spec_hash
from pal.core.turns import TurnContinuation
from pal.llm.ir import MessageState
from pal.memory import MemoryService
from pal.memory.turn_ir import L1TurnState
from pal.bunshin.checkpoint import AgentSessionCheckpointError, open_agent_session_checkpoint, seal_agent_session_checkpoint
from pal.bunshin.v2.role_contracts import role_session_stage_key
from pal.plugins.l3 import MockL3Plugin
from pal.shared import BunshinInvocationPack
from pal.bunshin.runner_components.artifacts import Artifacts
from pal.bunshin.runner_components.completion import Completion
from pal.bunshin.runner_components.memory_results import MemoryResults
from pal.bunshin.runner_components.research_budget import ResearchBudget
from pal.bunshin.runner_components.review_evidence import ReviewEvidence
from pal.bunshin.runner_components.tool_session import ToolSession


@dataclass
class SessionCheckpoints:
    artifacts: Artifacts
    completion: Completion
    memory_results: MemoryResults
    research_budget: ResearchBudget
    review_evidence: ReviewEvidence
    tool_session: ToolSession
    pack: BunshinInvocationPack
    runtime_root: Path
    agent_session_checkpoint: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    def continuation_is_restart_safe(
        self,
        continuation: TurnContinuation,
        memory_service: MemoryService | None = None,
    ) -> bool:
        if self.tool_session.execution_sessions is not None and self.tool_session.execution_sessions.resources_live:
            return False
        if continuation.pending_tool_call_batch or continuation.pending_tool_results:
            return False
        if memory_service is None:
            return True
        for turn in memory_service.history.turns:
            if turn.state != L1TurnState.ACTIVE:
                continue
            if turn.pending_call_ids:
                return False
            if any(message.state == MessageState.IN_PROGRESS for message in turn.messages):
                return False
        return True

    def load_agent_session_checkpoint(
        self,
        workspace: dict[str, Any],
        *,
        session_id: str,
        bundle: BunshinRuntimeBundle,
    ) -> dict[str, Any]:
        if not session_id:
            return {}
        session_metadata = dict((self.pack.metadata or {}).get("agent_session") or {})
        restore_text = str(session_metadata.get("continuation_input_path") or "").strip()
        if not restore_text:
            return {}
        restore_path = Path(restore_text)
        if not restore_path.is_file():
            raise AgentSessionCheckpointError(
                "manager-selected agent continuation is unavailable"
            )
        try:
            value = json.loads(restore_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentSessionCheckpointError(
                "manager-selected agent continuation is unreadable"
            ) from exc
        if not isinstance(value, dict) or str(value.get("logical_coroutine_id") or "") != session_id:
            raise AgentSessionCheckpointError(
                "manager-selected agent continuation has the wrong session identity"
            )
        value = open_agent_session_checkpoint(self.runtime_root, value)
        expected = self.agent_session_static_identity(bundle)
        if str(value.get("workflow_id") or "") != expected["workflow_id"]:
            raise AgentSessionCheckpointError(
                "manager-selected agent continuation has the wrong workflow"
            )
        if str(value.get("stage_key") or "") != expected["stage_key"]:
            raise AgentSessionCheckpointError(
                "manager-selected agent continuation has the wrong stage"
            )
        if str(value.get("runtime_spec_hash") or "") != expected["runtime_spec_hash"]:
            raise AgentSessionCheckpointError(
                "manager-selected agent continuation has the wrong runtime specification"
            )
        self.agent_session_checkpoint = dict(value)
        return dict(value)

    def agent_session_static_identity(
        self,
        bundle: BunshinRuntimeBundle,
    ) -> dict[str, str]:
        session_metadata = dict((self.pack.metadata or {}).get("agent_session") or {})
        binding = dict((self.pack.metadata or {}).get("bunshin_v2") or {})
        workflow_id = str(
            session_metadata.get("workflow_id")
            or binding.get("workflow_id")
            or ""
        ).strip()
        scope_kind = str(session_metadata.get("scope_kind") or "").strip()
        subject_key = str(session_metadata.get("subject_key") or "").strip()
        stage_key = str(session_metadata.get("stage_key") or "").strip()
        if not stage_key:
            stage_key = role_session_stage_key(
                scope_kind,
                subject_key,
                str(binding.get("role") or "").strip(),
            )
        if not workflow_id or not stage_key:
            raise AgentSessionCheckpointError(
                "agent session metadata has no stable workflow/stage identity"
            )
        spec_hash = runtime_spec_hash(
            bundle.module_registry,
            identity_parts={
                "family_binding_sha": str(binding.get("family_binding_sha") or ""),
                "role": str(binding.get("role") or ""),
                "profile": str(self.pack.bunshin_profile or ""),
                "harness_id": str(session_metadata.get("harness_id") or "pal"),
                "harness_generation": str(
                    session_metadata.get("harness_generation") or ""
                ),
            },
        )
        return {
            "workflow_id": workflow_id,
            "stage_key": stage_key,
            "runtime_spec_hash": spec_hash,
        }

    async def persist_agent_session_checkpoint(
        self,
        bundle: BunshinRuntimeBundle,
        state: BunshinAgentLoopState,
        continuation: TurnContinuation,
        *,
        initial_instruction: str,
        response_keys: list[str],
    ) -> None:
        session_metadata = dict((self.pack.metadata or {}).get("agent_session") or {})
        session_id = str(session_metadata.get("session_id") or "").strip()
        fencing_token = int(session_metadata.get("fencing_token") or 0)
        checkpoint_text = str(session_metadata.get("continuation_output_path") or "").strip()
        if not session_id or fencing_token <= 0 or not checkpoint_text:
            return
        checkpoint_path = Path(checkpoint_text)
        if not self.continuation_is_restart_safe(continuation, state.memory_service):
            return
        static_identity = self.agent_session_static_identity(bundle)
        sequence = max(
            0,
            int(self.agent_session_checkpoint.get("sequence") or 0),
        ) + 1
        identity = RuntimeSnapshotIdentity(
            logical_coroutine_id=session_id,
            workflow_id=static_identity["workflow_id"],
            stage_key=static_identity["stage_key"],
            sequence=sequence,
            producer_fencing_token=fencing_token,
            runtime_spec_hash=static_identity["runtime_spec_hash"],
        )
        runtime_snapshot = await bundle.runtime_state_coordinator.snapshot(identity)
        private_payload = {
            **identity.to_dict(),
            "coroutine_state": {
                "initial_instruction": str(initial_instruction),
                "response_keys": list(response_keys),
                "active_input_id": str(
                    getattr(
                        getattr(continuation, "opening_event", None),
                        "event_id",
                        "",
                    )
                    or ""
                ),
                "llm_round_count": int(state.llm_round_count),
                "tool_call_count": int(state.tool_call_count),
                "output_length_recovery_count": int(
                    getattr(state, "output_length_recovery_count", 0) or 0
                ),
                "pending_output_length_recovery_note": str(
                    getattr(state, "pending_output_length_recovery_note", "") or ""
                ),
                "tool_batch_count": int(continuation.tool_batch_count),
                "preferred_llm_endpoint_id": str(
                    continuation.preferred_llm_endpoint_id or ""
                ),
                "preferred_llm_model_id": str(
                    continuation.preferred_llm_model_id or ""
                ),
                "active_response_key": (
                    str(response_keys[-1]) if response_keys else ""
                ),
                "invocation_state": {
                    "produced_artifacts": [
                        dict(item) for item in self.artifacts.produced_artifacts
                    ],
                    "memory_candidate_records": [
                        dict(item)
                        for item in list(
                            getattr(state.memory_candidate_sink, "records", ()) or ()
                        )
                        if isinstance(item, Mapping)
                    ],
                    "review_tool_evidence_refs": [
                        dict(item) for item in self.review_evidence.review_tool_evidence_refs
                    ],
                    "web_research_usage": {
                        str(key): max(0, int(value))
                        for key, value in self.research_budget.web_research_usage.items()
                    },
                    "manager_submission_receipt_observed": bool(
                        self.completion.manager_submission_receipt_observed
                    ),
                },
            },
            "runtime_snapshot": runtime_snapshot,
        }
        payload = seal_agent_session_checkpoint(self.runtime_root, private_payload)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        target = checkpoint_path
        temporary = target.parent / f".{target.name}.{os.getpid()}.{uuid4().hex}.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        self.agent_session_checkpoint = private_payload

    def restore_invocation_checkpoint_state(
        self,
        coroutine_state: Mapping[str, Any],
        *,
        active_response_key: str,
        memory_candidate_sink: MockL3Plugin,
    ) -> None:
        """Restore process-local state only for the same semantic assignment."""

        checkpoint_key = str(
            coroutine_state.get("active_response_key") or ""
        ).strip()
        current_key = str(active_response_key or "").strip()
        raw_state = coroutine_state.get("invocation_state")
        if not checkpoint_key or checkpoint_key != current_key or not isinstance(
            raw_state,
            Mapping,
        ):
            self.artifacts.restore([])
            self.memory_results.clear_candidates()
            self.review_evidence.restore([])
            self.research_budget.restore({})
            self.completion.restore_receipt(False)
            memory_candidate_sink.records.clear()
            return
        value = dict(raw_state)

        def object_list(field_name: str) -> list[dict[str, Any]]:
            raw = value.get(field_name, [])
            if not isinstance(raw, list) or any(
                not isinstance(item, Mapping) for item in raw
            ):
                raise AgentSessionCheckpointError(
                    f"agent continuation has invalid {field_name}"
                )
            return [dict(item) for item in raw]

        raw_usage = value.get("web_research_usage", {})
        if not isinstance(raw_usage, Mapping):
            raise AgentSessionCheckpointError(
                "agent continuation has invalid web_research_usage"
            )
        usage = {
            str(key): int(count)
            for key, count in raw_usage.items()
        }
        if any(not key or count < 0 for key, count in usage.items()):
            raise AgentSessionCheckpointError(
                "agent continuation has invalid web_research_usage"
            )
        self.artifacts.restore(object_list("produced_artifacts"))
        self.review_evidence.restore(object_list(
            "review_tool_evidence_refs"
        ))
        memory_candidate_sink.records[:] = object_list(
            "memory_candidate_records"
        )
        self.memory_results.collect_candidates(memory_candidate_sink)
        self.research_budget.restore(usage)
        self.completion.restore_receipt(bool(
            value.get("manager_submission_receipt_observed")
        ))

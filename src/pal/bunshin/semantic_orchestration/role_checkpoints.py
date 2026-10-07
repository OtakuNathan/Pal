from __future__ import annotations
from pal.bunshin.semantic_orchestration.callbacks import SkillInjector
from pal.bunshin.semantic_orchestration.role_environment import _workflow_skill_injections
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
from pal.bunshin.semantic_orchestration.workspace_safety import _safe_component
import contextlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.skill_context import normalized_skill_injection
from pal.bunshin.checkpoint import AgentSessionCheckpointError, LogicalCoroutineCheckpointStore, normalize_agent_session_checkpoint
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import SubmissionInvariantError
from pal.bunshin.paths import invocation_root
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.role_contracts import role_session_stage_key
from pal.bunshin.role_protocol import RoleSessionAction, RoleSessionState, stable_hash
from pal.foundation import utc_now
from pal.shared import BunshinInvocationPack


@dataclass
class RoleCheckpoints:
    artifacts: ContentAddressedArtifactStore
    inject_skill: SkillInjector | None
    repository: BunshinV2Repository
    runtime_root: Path

    def durable_assignment_prompt_ref(
        self,
        assignment: Mapping[str, Any],
    ) -> ArtifactRef | None:
        attempt_id = str(assignment.get("active_attempt_id") or "")
        if not attempt_id:
            return None
        attempt = self.repository.role_attempts.read_role_attempt(attempt_id)
        prompt_value = dict((attempt or {}).get("prompt_pack_ref") or {})
        if not prompt_value.get("sha256"):
            return None
        prompt_ref = _ref_from_mapping(prompt_value)
        if self.repository.artifacts.read_artifact_record(prompt_ref.sha256) is None:
            return None
        return prompt_ref

    def durable_session_skill_injections(
        self,
        *,
        workflow_id: str,
        session_id: str,
    ) -> list[dict[str, str]] | None:
        for assignment in self.repository.role_assignments.list_role_assignments(
            workflow_id=workflow_id,
        ):
            if str(assignment.get("session_id") or "") != str(session_id):
                continue
            prompt_ref = self.durable_assignment_prompt_ref(assignment)
            if prompt_ref is None:
                continue
            prompt = BunshinInvocationPack.from_dict(
                dict(self.artifacts.read_json(prompt_ref))
            )
            result: list[dict[str, str]] = []
            for item in list(
                dict(prompt.metadata or {}).get("initial_skill_injections") or []
            ):
                if not isinstance(item, Mapping):
                    continue
                normalized = normalized_skill_injection(item)
                if normalized is not None:
                    result.append(normalized)
            return result
        return None

    def role_session_skill_injections(
        self,
        *,
        request: Mapping[str, Any],
        workflow_id: str,
        session_id: str,
    ) -> list[dict[str, str]]:
        prior = self.durable_session_skill_injections(
            workflow_id=workflow_id,
            session_id=session_id,
        )
        if prior is not None:
            return prior
        return _workflow_skill_injections(request, self.inject_skill)

    def terminal_from_assignment_receipt(
        self,
        assignment: Mapping[str, Any],
        *,
        primary_artifact_name: str,
        summary: str,
        original_terminal: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_ref = dict(assignment.get("submission_artifact_ref") or {})
        if not artifact_ref:
            raise SubmissionInvariantError("role assignment has no submission artifact")
        submitted = self.artifacts.read_json(artifact_ref)
        if stable_hash(submitted) != str(assignment.get("submission_payload_hash") or ""):
            raise SubmissionInvariantError(
                "role assignment submission payload hash does not match its artifact"
            )
        record = self.repository.artifacts.read_artifact_record(str(artifact_ref.get("sha256") or ""))
        if record is None:
            raise SubmissionInvariantError("role assignment submission artifact is unavailable")
        primary = {
            "path": str(record["storage_path"]),
            "relative_path": primary_artifact_name,
            "title": "Durable role submission",
            "role": "primary",
            "mime_type": "application/json",
        }
        original_payload = dict(dict(original_terminal or {}).get("payload") or {})
        assignment_id = str(assignment.get("assignment_id") or "")
        payload_hash = str(assignment.get("submission_payload_hash") or "")
        if not assignment_id or not payload_hash:
            raise SubmissionInvariantError(
                "role assignment receipt has no durable assignment identity"
            )
        return {
            "event_kind": "terminal",
            "phase": "completed",
            "payload": {
                **original_payload,
                "status": "completed",
                "summary": str(summary or "Worker submission recorded."),
                # The assignment receipt durably owns exactly one primary
                # submission. Role-local supporting projections must be
                # reconstructed from their durable source, never retained as
                # invocation-directory paths in an otherwise replayable
                # terminal.
                "artifacts": [primary],
                "primary_artifact": primary,
                "submission_receipt": artifact_ref,
                "role_assignment_id": assignment_id,
                "role_submission_payload_hash": payload_hash,
                # Only receipt-only reconciliation is a replay. A fresh role
                # process also settles through the durable receipt, but its
                # original terminal carries the billable worker turn.
                "durable_receipt_replay": original_terminal is None,
                "session_turn_index": int(
                    original_payload.get("session_turn_index") or 0
                ),
                "v2_timing": dict(original_payload.get("v2_timing") or {}),
            },
        }

    def prepare_agent_session_attempt(
        self,
        *,
        session_id: str,
        attempt_id: str,
    ) -> tuple[Path | None, Path]:
        session = self.repository.role_sessions.read_role_session(session_id)
        if session is None:
            raise AgentSessionCheckpointError(
                f"role session disappeared before process start: {session_id}"
            )
        attempt_dir = (
            invocation_root(self.runtime_root)
            / session_id
            / "session-attempts"
            / _safe_component(attempt_id)
        )
        attempt_dir.mkdir(parents=True, exist_ok=True)
        restore_path = attempt_dir / "continuation-input.json"
        checkpoint_path = attempt_dir / "continuation-output.json"
        with contextlib.suppress(FileNotFoundError):
            restore_path.unlink()
        with contextlib.suppress(FileNotFoundError):
            checkpoint_path.unlink()

        store = LogicalCoroutineCheckpointStore(self.runtime_root)
        checkpoint = store.read(session_id)
        if checkpoint is None:
            if str(session.get("status") or "") != RoleSessionState.UNINITIALIZED.value:
                raise AgentSessionCheckpointError(
                    "role session requires a logical-coroutine checkpoint but it is missing"
                )
            return None, checkpoint_path
        restored = normalize_agent_session_checkpoint(checkpoint)
        if str(restored.get("logical_coroutine_id") or "") != session_id:
            raise AgentSessionCheckpointError(
                "role session continuation has the wrong session identity"
            )
        if str(restored.get("workflow_id") or "") != str(session.get("workflow_id") or ""):
            raise AgentSessionCheckpointError(
                "role session continuation has the wrong workflow"
            )
        expected_stage = role_session_stage_key(
            str(session.get("scope_kind") or ""),
            str(session.get("subject_key") or ""),
            str(session.get("role") or ""),
        )
        if str(restored.get("stage_key") or "") != expected_stage:
            raise AgentSessionCheckpointError(
                "role session continuation has the wrong stage"
            )
        materialized = store.materialize_input(session_id, restore_path)
        if materialized is None:
            raise AgentSessionCheckpointError("logical-coroutine checkpoint disappeared during restoration")
        # Reconcile a first publication whose file persisted but DB/ACK did
        # not. Never erase or bypass that continuation to fresh-start.
        with self.repository.database.write_connection() as connection:
            self.repository.role_sessions.transition_role_session_locked(
                connection, session_id, RoleSessionAction.INITIALIZE, now=utc_now(),
            )
        return materialized, checkpoint_path

    def publish_agent_session_checkpoint(
        self,
        invocation_id: str,
        fencing_token: int,
        checkpoint_path: Path,
    ) -> dict[str, Any] | None:
        if not checkpoint_path.is_file():
            return None
        try:
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("worker checkpoint output is unreadable") from exc
        if not isinstance(payload, dict) or str(payload.get("logical_coroutine_id") or "") != invocation_id:
            raise RuntimeError("worker checkpoint output has the wrong session identity")
        try:
            payload = normalize_agent_session_checkpoint(payload)
        except AgentSessionCheckpointError as exc:
            raise RuntimeError("worker checkpoint output has an invalid envelope") from exc
        if int(payload.get("producer_fencing_token") or 0) != int(fencing_token):
            raise RuntimeError("worker checkpoint output has a stale fencing token")
        session = self.repository.role_sessions.read_role_session(invocation_id)
        if session is None:
            raise RuntimeError("worker checkpoint output has no durable session")
        if str(payload.get("workflow_id") or "") != str(session.get("workflow_id") or ""):
            raise RuntimeError("worker checkpoint output has the wrong workflow")
        expected_stage = role_session_stage_key(
            str(session.get("scope_kind") or ""),
            str(session.get("subject_key") or ""),
            str(session.get("role") or ""),
        )
        if str(payload.get("stage_key") or "") != expected_stage:
            raise RuntimeError("worker checkpoint output has the wrong stage")
        with self.repository.database.write_connection() as connection:
            self.repository.role_sessions.publish_role_session_checkpoint_locked(
                connection,
                session_id=invocation_id,
                fencing_token=fencing_token,
                checkpoint=payload,
            )
        with contextlib.suppress(FileNotFoundError):
            checkpoint_path.unlink()
        return payload

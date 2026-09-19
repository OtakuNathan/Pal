"""Q12/R08: attachment leases across compaction.

Q12 — artifacts referenced by durably staged pending events survive the
reap ticks that run while a compaction holds the gate; the staging file
carries references only, never bytes.

R08 — artifacts referenced from the committed L1/seed state survive the
post-install cleanup tick and a restart (fresh manager over the same
repository) still finds the owner-scoped handle; an existing handle is
never re-requested as a new resource.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from pal.artifact.models import (
    ArtifactHotStateModel,
    ArtifactRecordModel,
    ArtifactRepresentationModel,
)
from pal.artifact.service import ArtifactManager
from pal.artifact.repository import ArtifactRepository
from pal.core.ingress_staging import IngressStagingStore, StagedIngressRecord
from pal.core.runtime import PalCore
from pal.foundation import PalV2Database
from pal.llm.ir import ArtifactRefPartIR, LLMMessageIR, MessageRole, TextPartIR
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from pal.shared import RuntimeStatus

from tests.test_compaction_gate import _BarrierEngine, _envelope
from tests.test_runtime_compaction import _memory_with_turns

SCOPE = "pal:resident"


def _ingest_artifact(manager: ArtifactManager, tmp: Path, name: str) -> str:
    path = tmp / name
    path.write_bytes(f"content-of-{name}".encode())
    ref = manager.register_ingested(
        path,
        scope_key=SCOPE,
        turn_id="t-lease",
        source_channel="test",
    )
    return str(ref.artifact_id)


def _compact_core(tmp: Path, engine_status: str = "compacted"):
    core = PalCore()
    service = _memory_with_turns(2)
    database = PalV2Database(tmp / "artifacts.sqlite3")
    database.initialize([
        ArtifactRecordModel, ArtifactRepresentationModel, ArtifactHotStateModel,
    ])
    manager = ArtifactManager(
        runtime_root=tmp, repository=ArtifactRepository(),
    )
    core.context.port_registry["memory:memory"] = service
    core.context.port_registry["llm:llm"] = object()
    core.context.port_registry["artifact:artifact"] = manager
    core.turn_executor._compaction_engine = _BarrierEngine(status=engine_status)
    core.state.ingress_staging = IngressStagingStore(tmp / "staging.json")
    return core, service, manager


def _effect():
    from pal.core.turns import MemoryCompactEffect

    return MemoryCompactEffect(
        assembly_context=None, target_input_budget=8_192, reserved_output_tokens=2_048,
    )


def test_q12_staged_attachment_lease_survives_reap_across_compaction():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            core, service, manager = _compact_core(tmp)
            artifact_id = _ingest_artifact(manager, tmp, "lease-q12.txt")

            envelope = _envelope("m-q12", "see attachment")
            envelope.event.payload["attachments"] = [
                {"artifact_id": artifact_id, "kind": "file"},
            ]
            core.state.ingress_staging.enqueue(
                StagedIngressRecord.from_channel_envelope(envelope, scope=SCOPE)
            )
            core.state.pending_channel_turns.append(envelope)

            continuation = SimpleNamespace(
                turn_id="t-lease", waiting_effect_id=None,
                interrupted=False, interrupt_reason="",
                delivery_binding=None,
                pending_compact_memory_candidate_batches=[],
            )
            task = asyncio.ensure_future(
                core.turn_executor.execute_turn_effect_async(continuation, _effect())
            )
            engine = core.turn_executor._compaction_engine
            await engine.entered.wait()

            # Ordinary reap tick while the compaction holds the gate: the
            # staged reference keeps the artifact's lease alive.
            reaped = manager.reap_expired()
            assert reaped["retired"] == 0
            hot = manager.select(artifact_id, SCOPE)
            assert hot["ttl_refreshed"] is True
            # While the event is still durably staged, the file carries the
            # reference only — never the artifact bytes.
            staged_text = (tmp / "staging.json").read_text(encoding="utf-8")
            assert artifact_id in staged_text
            assert "content-of-lease-q12" not in staged_text

            engine.release.set()
            result = await task
            assert result.status == RuntimeStatus.OK

            # After the compaction (and the queued-input drain it triggers)
            # the referenced content is still readable.
            manager.select(artifact_id, SCOPE)
    asyncio.run(scenario())


def test_r08_seed_referenced_artifact_survives_cleanup_and_restart():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            core, service, manager = _compact_core(tmp)
            artifact_id = _ingest_artifact(manager, tmp, "lease-r08.txt")

            # The committed L1 (seed/active input shape) references the
            # artifact from a message part.
            service.l1_store.turns.append(L1TurnIR(
                turn_id="t-lease",
                state=L1TurnState.ACTIVE,
                messages=[LLMMessageIR(
                    role=MessageRole.USER,
                    parts=(
                        TextPartIR("run with the artifact"),
                        ArtifactRefPartIR(
                            artifact_id=artifact_id,
                            kind="file",
                            file_name="lease-r08.txt",
                        ),
                    ),
                    message_id="u1",
                )],
            ))
            continuation = SimpleNamespace(
                turn_id="t-lease", waiting_effect_id=None,
                interrupted=False, interrupt_reason="",
                delivery_binding=None,
                pending_compact_memory_candidate_batches=[],
            )
            task = asyncio.ensure_future(
                core.turn_executor.execute_turn_effect_async(continuation, _effect())
            )
            engine = core.turn_executor._compaction_engine
            await engine.entered.wait()
            engine.release.set()
            result = await task
            assert result.status == RuntimeStatus.OK

            # Post-install cleanup tick: the L1-referenced handle survives.
            assert manager.reap_expired()["retired"] == 0

            # Restart: a fresh database+repository over the same files
            # still finds the owner-scoped handle and never re-requests or
            # deletes it.
            restarted_db = PalV2Database(tmp / "artifacts.sqlite3")
            restarted_db.initialize([
                ArtifactRecordModel, ArtifactRepresentationModel, ArtifactHotStateModel,
            ])
            restarted = ArtifactManager(
                runtime_root=tmp, repository=ArtifactRepository(),
            )
            hot = restarted.select(artifact_id, SCOPE)
            assert hot["artifact"]["artifact_id"] == artifact_id
            records = [
                record for record in restarted.repository.list_records()
                if record.artifact_id == artifact_id
            ]
            assert len(records) == 1  # not re-registered as a new resource
    asyncio.run(scenario())

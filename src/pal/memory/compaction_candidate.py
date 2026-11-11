"""A single validated compaction candidate for text and opaque history."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from pal.llm.ir import ReplayEnvelope
from pal.memory.contracts import L2Entry
from pal.shared.json_values import thaw_json


@dataclass(frozen=True)
class CompactionCandidate:
    checkpoint: L2Entry | None = None
    native_window: ReplayEnvelope | None = None
    source_ids: tuple[str, ...] = ()
    retained_result_refs: tuple[str, ...] = ()
    usage: Mapping[str, Any] = field(default_factory=dict)
    result_snapshots: tuple[Mapping[str, Any], ...] = ()

    def as_entry(self) -> L2Entry:
        if (self.checkpoint is None) == (self.native_window is None):
            raise ValueError("compaction requires exactly one text or native checkpoint")
        if self.checkpoint is not None:
            return self.checkpoint
        replay = self.native_window
        assert replay is not None
        # Storage's existing install/archive transaction accepts L2Entry. The
        # builder extracts this envelope into the L1 message replay, so it is
        # persisted once and never emitted as a textual summary or proposal.
        return L2Entry(
            entry_id="memory_summary_current", kind="summary", scope="system",
            title="Compacted history", summary="Native compacted history", rendered="Native compacted history",
            source_kind="l1_compaction", payload={
                "native_window": {"wire_shape": replay.wire_shape.value,
                    "endpoint_id": replay.endpoint_id, "model_id": replay.model_id,
                    "payload": thaw_json(replay.payload)},
                "compact_source_ids": list(self.source_ids),
                "retained_result_refs": list(self.retained_result_refs),
                "result_snapshots": [thaw_json(ref) for ref in self.result_snapshots],
            })

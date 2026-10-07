from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.repository import BunshinV2Repository


@dataclass
class EffectReads:
    repository: BunshinV2Repository

    def effect_causal_context(
        self,
        effect: Mapping[str, Any],
    ) -> dict[str, Any]:
        embedded = dict(dict(effect.get("payload") or {}).get("_causal_context") or {})
        if embedded:
            return embedded
        return self.repository.queries.read_domain_event_effect_context(
            str(effect.get("event_id") or "")
        )

    @staticmethod
    def effect_role_mode(effect: Mapping[str, Any]) -> str:
        return str(
            effect.get("role_mode")
            or dict(effect.get("payload") or {}).get("role_mode")
            or ""
        )

    def effect_snapshot(self, effect: Mapping[str, Any]) -> AggregateSnapshot:
        aggregate_type = AggregateType(str(effect["aggregate_type"]))
        snapshot = self.repository.snapshots.read_snapshot(aggregate_type, str(effect["aggregate_id"]))
        if snapshot is None:
            raise ValueError("semantic effect aggregate no longer exists")
        return snapshot

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping
from pal.bunshin.v2.contracts import AggregateType
from pal.bunshin.v2.semantic_orchestration.aggregate_control import AggregateControl
from pal.bunshin.v2.semantic_orchestration.node_control import NodeControl


@dataclass
class RoleControl:
    aggregate_control: AggregateControl
    node_control: NodeControl

    async def pause_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(effect.get("aggregate_type") or "") == AggregateType.DAG_NODE_RUN.value:
            return await self.node_control.stop_node_worker(effect, cancel=False)
        return await self.aggregate_control.stop_aggregate_worker(effect, cancel=False)

    async def cancel_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(effect.get("aggregate_type") or "") == AggregateType.DAG_NODE_RUN.value:
            return await self.node_control.stop_node_worker(effect, cancel=True)
        return await self.aggregate_control.stop_aggregate_worker(effect, cancel=True)

    async def quiesce_role_for_triage(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(effect.get("aggregate_type") or "") == AggregateType.DAG_NODE_RUN.value:
            return await self.node_control.stop_node_worker(effect, cancel=False, confirm=False)
        return await self.aggregate_control.stop_aggregate_worker(effect, cancel=False, confirm=False)

    async def resume_semantic_state(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(effect.get("aggregate_type") or "") == AggregateType.DAG_NODE_RUN.value:
            return await self.node_control.resume_node(effect)
        return await self.aggregate_control.resume_aggregate(effect)

    async def reconcile_semantic_state(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(effect.get("aggregate_type") or "") == AggregateType.DAG_NODE_RUN.value:
            return await self.node_control.reconcile_node(effect)
        return await self.aggregate_control.resume_aggregate(effect)

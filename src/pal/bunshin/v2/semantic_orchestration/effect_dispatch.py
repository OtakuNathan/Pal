from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping
from pal.bunshin.v2.background_assignments import BackgroundAssignments
from pal.bunshin.v2.role_contracts import RoleMode
from pal.bunshin.v2.semantic_orchestration.assignment_execution import AssignmentExecution
from pal.bunshin.v2.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.v2.semantic_orchestration.routes import SEMANTIC_EFFECT_ROUTES


@dataclass
class EffectDispatch:
    assignment_execution: AssignmentExecution
    effect_reads: EffectReads
    background: BackgroundAssignments
    handlers: Mapping[str, Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]]]

    async def execute_semantic_effect(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        effect_type = str(effect.get("effect_type") or "")
        route = SEMANTIC_EFFECT_ROUTES.get(effect_type)
        if route is None:
            raise RuntimeError(f"semantic effect is not implemented: {effect_type}")
        snapshot = self.effect_reads.effect_snapshot(effect)
        causal = self.effect_reads.effect_causal_context(effect)
        target_state = str(causal.get("target_state") or "")
        causal_version = int(causal.get("aggregate_version") or 0)
        if target_state and snapshot.state != target_state:
            return {
                "status": "superseded",
                "aggregate_state": snapshot.state,
                "causal_target_state": target_state,
            }
        if causal_version and snapshot.version != causal_version:
            causal_owner = str(causal.get("active_worker_id") or "")
            same_owner = bool(
                causal_owner
                and causal_owner
                == str(snapshot.payload.get("active_worker_id") or "")
                and str(causal.get("lease_resource_key") or "")
                == str(snapshot.payload.get("lease_resource_key") or "")
                and int(causal.get("fencing_token") or 0)
                == int(snapshot.payload.get("fencing_token") or 0)
            )
            if not same_owner:
                return {
                    "status": "superseded",
                    "aggregate_state": snapshot.state,
                    "aggregate_version": snapshot.version,
                    "causal_aggregate_version": causal_version,
                }
        mode = self.effect_reads.effect_role_mode(effect)
        if mode and route.modes and RoleMode(mode) not in route.modes:
            raise ValueError(f"{effect_type} does not support role mode {mode}")
        handler = self.handlers[effect_type]
        if route.background:
            return await self.launch_background_worker(effect, handler)
        return dict(await handler(effect))

    async def launch_background_worker(self, effect, runner):
        return await self.background.launch(effect, runner, self.assignment_execution.background_worker_loop)

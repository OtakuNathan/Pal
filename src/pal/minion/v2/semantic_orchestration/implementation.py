from __future__ import annotations

from pal.minion.v2.role_contracts import OrchestrationRole, RoleMode
from pal.minion.v2.semantic_orchestration.contracts import SemanticEffectRoute


IMPLEMENTATION_EFFECT_ROUTES = {
    "admit_implementation_role": SemanticEffectRoute(
        "_admit_implementation_role",
        OrchestrationRole.IMPLEMENTATION,
        frozenset({RoleMode.PRODUCE, RoleMode.REPAIR}),
    ),
    "run_implementation_role": SemanticEffectRoute(
        "_run_implementation_role",
        OrchestrationRole.IMPLEMENTATION,
        frozenset({RoleMode.PRODUCE, RoleMode.REPAIR}),
        background=True,
    ),
    "quiesce_implementation_role": SemanticEffectRoute(
        "_quiesce_node",
        OrchestrationRole.IMPLEMENTATION,
        frozenset({RoleMode.PRODUCE, RoleMode.REPAIR}),
    ),
    "snapshot_implementation_result": SemanticEffectRoute("_snapshot_implementation_result"),
    "publish_final_deliverable": SemanticEffectRoute("_publish_final_deliverable"),
}

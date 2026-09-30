from __future__ import annotations

from pal.bunshin.v2.role_contracts import OrchestrationRole, RoleMode
from pal.bunshin.v2.semantic_orchestration.contracts import SemanticEffectRoute


IMPLEMENTATION_EFFECT_ROUTES = {
    "admit_implementation_role": SemanticEffectRoute(

        OrchestrationRole.IMPLEMENTATION,
        frozenset({RoleMode.PRODUCE, RoleMode.REPAIR}),
    ),
    "run_implementation_role": SemanticEffectRoute(

        OrchestrationRole.IMPLEMENTATION,
        frozenset({RoleMode.PRODUCE, RoleMode.REPAIR}),
        background=True,
    ),
    "quiesce_implementation_role": SemanticEffectRoute(

        OrchestrationRole.IMPLEMENTATION,
        frozenset({RoleMode.PRODUCE, RoleMode.REPAIR}),
    ),
    "snapshot_implementation_result": SemanticEffectRoute(),
    "publish_final_deliverable": SemanticEffectRoute(),
}

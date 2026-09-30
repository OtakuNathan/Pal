from __future__ import annotations

from pal.bunshin.v2.role_contracts import OrchestrationRole, RoleMode
from pal.bunshin.v2.semantic_orchestration.contracts import SemanticEffectRoute


ARCHITECTURE_EFFECT_ROUTES = {
    "admit_architect_role": SemanticEffectRoute(

        OrchestrationRole.ARCHITECT,
        frozenset({RoleMode.AUTHOR, RoleMode.REVISION}),
        background=True,
    ),
    "quiesce_architect_role": SemanticEffectRoute(

        OrchestrationRole.ARCHITECT,
        frozenset({RoleMode.AUTHOR, RoleMode.REVISION}),
    ),
    "snapshot_architect_result": SemanticEffectRoute(

        OrchestrationRole.ARCHITECT,
        frozenset({RoleMode.AUTHOR, RoleMode.REVISION}),
    ),
    "publish_architecture_review_request": SemanticEffectRoute(

    ),
    "materialize_plan_revision": SemanticEffectRoute(),
}

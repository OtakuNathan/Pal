from __future__ import annotations

from pal.minion.v2.role_contracts import OrchestrationRole, RoleMode
from pal.minion.v2.semantic_orchestration.contracts import SemanticEffectRoute


ARCHITECTURE_EFFECT_ROUTES = {
    "admit_architect_role": SemanticEffectRoute(
        "_run_architecture_stage",
        OrchestrationRole.ARCHITECT,
        frozenset({RoleMode.AUTHOR, RoleMode.REVISION}),
        background=True,
    ),
    "quiesce_architect_role": SemanticEffectRoute(
        "_quiesce_architect_role",
        OrchestrationRole.ARCHITECT,
        frozenset({RoleMode.AUTHOR, RoleMode.REVISION}),
    ),
    "snapshot_architect_result": SemanticEffectRoute(
        "_snapshot_architect_result",
        OrchestrationRole.ARCHITECT,
        frozenset({RoleMode.AUTHOR, RoleMode.REVISION}),
    ),
    "publish_architecture_review_request": SemanticEffectRoute(
        "_publish_human_architecture_review"
    ),
    "materialize_plan_revision": SemanticEffectRoute("_materialize_plan_revision_status"),
}

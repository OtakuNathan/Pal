from __future__ import annotations

from pal.minion.v2.role_contracts import OrchestrationRole, RoleMode
from pal.minion.v2.semantic_orchestration.contracts import SemanticEffectRoute


REVIEW_EFFECT_ROUTES = {
    "admit_reviewer_role": SemanticEffectRoute(
        "_admit_reviewer_role",
        OrchestrationRole.REVIEWER,
        frozenset({RoleMode.STANDALONE}),
    ),
    "run_reviewer_role": SemanticEffectRoute(
        "_run_reviewer_role",
        OrchestrationRole.REVIEWER,
        frozenset({RoleMode.ARCHITECTURE, RoleMode.STANDALONE}),
        background=True,
    ),
    "publish_review_report": SemanticEffectRoute("_publish_standalone_report"),
}

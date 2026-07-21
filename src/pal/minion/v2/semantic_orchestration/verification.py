from __future__ import annotations

from pal.minion.v2.role_contracts import OrchestrationRole, RoleMode
from pal.minion.v2.semantic_orchestration.contracts import SemanticEffectRoute


VERIFICATION_EFFECT_ROUTES = {
    "admit_verifier_role": SemanticEffectRoute(
        "_admit_verifier_role",
        OrchestrationRole.VERIFIER,
        frozenset({RoleMode.MODULE, RoleMode.SCENARIO}),
    ),
    "run_verifier_role": SemanticEffectRoute(
        "_run_verification_role",
        OrchestrationRole.VERIFIER,
        frozenset({RoleMode.MODULE, RoleMode.SCENARIO}),
        background=True,
    ),
    "quiesce_verifier_role": SemanticEffectRoute(
        "_quiesce_verifier_role",
        OrchestrationRole.VERIFIER,
        frozenset({RoleMode.MODULE, RoleMode.SCENARIO}),
    ),
    "snapshot_verifier_result": SemanticEffectRoute("_snapshot_semantic_verification"),
}

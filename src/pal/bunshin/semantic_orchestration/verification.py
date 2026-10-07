from __future__ import annotations

from pal.bunshin.role_contracts import OrchestrationRole, RoleMode
from pal.bunshin.semantic_orchestration.contracts import SemanticEffectRoute


VERIFICATION_EFFECT_ROUTES = {
    "reconcile_dependency_repairs": SemanticEffectRoute(),
    "admit_verifier_role": SemanticEffectRoute(

        OrchestrationRole.VERIFIER,
        frozenset({RoleMode.MODULE}),
    ),
    "run_verifier_role": SemanticEffectRoute(

        OrchestrationRole.VERIFIER,
        frozenset({RoleMode.MODULE}),
        background=True,
    ),
    "quiesce_verifier_role": SemanticEffectRoute(

        OrchestrationRole.VERIFIER,
        frozenset({RoleMode.MODULE}),
    ),
    "snapshot_verifier_result": SemanticEffectRoute(),
}

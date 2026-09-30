from __future__ import annotations
from pal.bunshin.v2.semantic_orchestration.architecture import ARCHITECTURE_EFFECT_ROUTES
from pal.bunshin.v2.semantic_orchestration.contracts import SemanticEffectRoute, merge_effect_routes
from pal.bunshin.v2.semantic_orchestration.implementation import IMPLEMENTATION_EFFECT_ROUTES
from pal.bunshin.v2.semantic_orchestration.review import REVIEW_EFFECT_ROUTES
from pal.bunshin.v2.semantic_orchestration.verification import VERIFICATION_EFFECT_ROUTES


CONTROL_EFFECT_ROUTES = {
    "pause_role": SemanticEffectRoute(),
    "cancel_role": SemanticEffectRoute(),
    "quiesce_role_for_triage": SemanticEffectRoute(),
    "resume_semantic_state": SemanticEffectRoute(),
    "reconcile_semantic_state": SemanticEffectRoute(),
}

SEMANTIC_EFFECT_ROUTES = merge_effect_routes(
    ARCHITECTURE_EFFECT_ROUTES,
    REVIEW_EFFECT_ROUTES,
    IMPLEMENTATION_EFFECT_ROUTES,
    VERIFICATION_EFFECT_ROUTES,
    CONTROL_EFFECT_ROUTES,
)

SEMANTIC_EFFECT_TYPES = frozenset(SEMANTIC_EFFECT_ROUTES)

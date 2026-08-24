from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from pal.core.module_registry import MODULE_TIER_CORE_FOUNDATION, ModuleHandle
from pal.execution.tool_facade import ToolGuidance
from pal.identity.service import IdentityService
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


@dataclass(frozen=True)
class IdentitySnapshot:
    has_persona: bool
    has_preferences: bool
    persona: dict[str, Any] | None = None
    preferences: dict[str, Any] | None = None
    mounted: bool = True
    degraded: bool = False


@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:identity",
    target_kind="module",
)
@dataclass
class IdentityIntrospectionProvider:
    service: IdentityService
    module_id: str = "identity"

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="module",
        action_name="show",
        guidance=ToolGuidance(
            purpose="Read Pal's configured identity and preferences from durable storage and refresh the resident in-memory projection.",
            use_when="Inspecting Pal's configured persona/preferences or explicitly refreshing identity after external configuration changes.",
            do_not_use_when="Recalling user facts (use recall_memory). Checking behavior routing (use behavior_show).",
            failure_next_steps="Read-only. If no persona, identity prompt fragments will use defaults.",
        ),
        aliases=("identity_show",),
    )
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        snapshot = inspect_identity(self)
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="identity snapshot",
            structured=snapshot.__dict__,
            llm_text=render_titled_structured_for_llm("Identity snapshot", snapshot.__dict__),
        )


def inspect_identity(provider: IdentityIntrospectionProvider) -> IdentitySnapshot:
    service = provider.service
    persona, preferences = service.refresh_projection()
    return IdentitySnapshot(
        has_persona=persona is not None,
        has_preferences=preferences is not None,
        persona=asdict(persona) if persona is not None else None,
        preferences=asdict(preferences) if preferences is not None else None,
        mounted=True,
        degraded=False,
    )


def register_with_core(context: MainContext, service: IdentityService) -> ModuleHandle:
    from pal.identity.prompt import IdentityPromptFragmentProvider

    service.refresh_projection()
    provider = IdentityIntrospectionProvider(service=service)
    prompt_provider = IdentityPromptFragmentProvider(service=service)
    handle = ModuleHandle(
        module_id="identity",
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        supports_lifecycle_capabilities=False,
        ports={"identity": service},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle

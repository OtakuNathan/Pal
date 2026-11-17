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
    async def inspect_configuration(params):
        return configuration_snapshot(service, params)

    async def refresh_configuration(params):
        if params:
            raise ValueError("refresh takes no arguments")
        service.refresh_projection()
        return configuration_snapshot(service, {})

    handle = ModuleHandle(
        module_id="identity",
        configuration_handlers={"identity.inspect": inspect_configuration,
                                "identity.refresh": refresh_configuration},
        tier=MODULE_TIER_CORE_FOUNDATION,
        detachable=False,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        ports={"identity": service},
    )
    context.register_module(handle)
    context.prompt_fragment_registry.register(prompt_provider)
    return handle


def configuration_snapshot(service: IdentityService, params: dict) -> dict:
    if params:
        raise ValueError("inspect takes no arguments")
    persona, preferences = service.get_persona(), service.get_preferences()
    return {"status": "ok", "activation": "live", "refresh_scope": "resident_projection",
            "startup_pinned_fields": ["display_name", "core_policy"],
            "state": {"has_persona": persona is not None, "has_preferences": preferences is not None,
                      "persona": asdict(persona) if persona is not None else None,
                      "preferences": asdict(preferences) if preferences is not None else None}}

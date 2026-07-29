from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel

from pal.execution.contracts import CapabilityCall, CapabilityDescriptor, CapabilityResult
from pal.execution.tool_facade import (
    EmptyToolInput,
    ProviderPayloadOutput,
    ToolExecutionSemantics,
    ToolGuidance,
)


INTROSPECTION_NAMESPACE = "introspection"
OPERATION_NAMESPACE = "operation"
# Module-level actions are routed through the exact same O(1) dispatch table as
# instance-level actions. Using a hard sentinel keeps the key shape uniform.
SINGLETON_TARGET = "__singleton__"


@dataclass(frozen=True)
class CapabilityNodeBlueprint:
    # A blueprint is import-time metadata only. It is not yet a runtime node.
    namespace: str
    scope: str
    kind: str
    source: str
    target_kind: str
    path_module_id: str | None = None
    iterable_resolver: str | None = None
    target_id_resolver: str | None = None
    target_label_resolver: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityActionBlueprint:
    namespace: str
    scope: str
    action_name: str
    handler_name: str
    async_handler_name: str | None = None
    family: str = ""
    description: str = ""
    aliases: tuple[str, ...] = ()
    InputModel: type[BaseModel] = EmptyToolInput
    OutputModel: type[BaseModel] = ProviderPayloadOutput
    guidance: ToolGuidance | None = None
    execution: ToolExecutionSemantics | None = None
    search_text: str = ""
    examples: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HydratedCapabilityNode:
    # Hydrated nodes are the runtime truth. They also record exactly which
    # dispatch/search entries were registered so subtree teardown can delete
    # them precisely instead of scanning whole tables.
    node_id: str
    namespace: str
    scope: str
    kind: str
    module_id: str
    source: str
    target_kind: str
    target_id: str
    target_label: str
    metadata: dict[str, Any] = field(default_factory=dict)
    children: list[str] = field(default_factory=list)
    bound_action_keys: list[tuple[str, str]] = field(default_factory=list)
    search_record_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BoundCapabilityAction:
    canonical_path: str
    target_id: str
    descriptor: CapabilityDescriptor
    callable: Callable[[CapabilityCall], CapabilityResult]
    async_callable: Callable[[CapabilityCall], Any] | None = None


@dataclass
class MountedSubtreeHandle:
    # A mounted subtree handle is the teardown contract for one hydration
    # result. PalCore keeps governance, but teardown is driven by these exact
    # recorded keys/ids.
    module_id: str
    nodes: list[HydratedCapabilityNode] = field(default_factory=list)
    descriptors: list[CapabilityDescriptor] = field(default_factory=list)
    bound_actions: list[BoundCapabilityAction] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)
    bound_action_keys: list[tuple[str, str]] = field(default_factory=list)
    search_record_ids: list[str] = field(default_factory=list)
    mounted: bool = False


def capability_node(
    *,
    namespace: str,
    scope: str,
    kind: str,
    source: str,
    target_kind: str,
    path_module_id: str | None = None,
    iterable_resolver: str | None = None,
    target_id_resolver: str | None = None,
    target_label_resolver: str | None = None,
    metadata: dict[str, Any] | None = None,
):
    def decorator(cls):
        existing = list(getattr(cls, "__capability_node_blueprints__", ()))
        existing.append(
            CapabilityNodeBlueprint(
                namespace=namespace,
                scope=scope,
                kind=kind,
                source=source,
                target_kind=target_kind,
                path_module_id=path_module_id,
                iterable_resolver=iterable_resolver,
                target_id_resolver=target_id_resolver,
                target_label_resolver=target_label_resolver,
                metadata=dict(metadata or {}),
            )
        )
        cls.__capability_node_blueprints__ = existing
        return cls

    return decorator


def capability_action(
    *,
    namespace: str,
    scope: str,
    action_name: str,
    family: str = "",
    description: str = "",
    aliases: tuple[str, ...] = (),
    InputModel: type[BaseModel] = EmptyToolInput,
    OutputModel: type[BaseModel] = ProviderPayloadOutput,
    guidance: ToolGuidance | None = None,
    execution: ToolExecutionSemantics | None = None,
    search_text: str = "",
    examples: tuple[dict[str, Any], ...] = (),
    async_handler_name: str | None = None,
    metadata: dict[str, Any] | None = None,
):
    def decorator(fn):
        existing = list(getattr(fn, "__capability_action_blueprints__", ()))
        existing.append(
            CapabilityActionBlueprint(
                namespace=namespace,
                scope=scope,
                action_name=action_name,
                handler_name=fn.__name__,
                async_handler_name=str(async_handler_name or "").strip() or None,
                family=family,
                description=description,
                aliases=tuple(aliases),
                InputModel=InputModel,
                OutputModel=OutputModel,
                guidance=guidance,
                execution=execution,
                search_text=str(search_text or ""),
                examples=tuple(dict(item) for item in examples),
                metadata=dict(metadata or {}),
            )
        )
        fn.__capability_action_blueprints__ = existing
        return fn

    return decorator

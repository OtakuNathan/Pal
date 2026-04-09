from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from pal.execution.contracts import CapabilityCall, CapabilityDescriptor, CapabilityResult


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
    family: str = ""
    description: str = ""
    aliases: tuple[str, ...] = ()
    args_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)
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


@dataclass
class CapabilityForestRegistry:
    nodes: dict[str, HydratedCapabilityNode] = field(default_factory=dict)
    by_module: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def mount(self, subtree: MountedSubtreeHandle) -> None:
        for node in subtree.nodes:
            self.nodes[node.node_id] = node
            bucket = self.by_module[node.module_id]
            if node.node_id not in bucket:
                bucket.append(node.node_id)
        subtree.mounted = True

    def unmount(self, subtree: MountedSubtreeHandle) -> None:
        for node_id in subtree.node_ids:
            node = self.nodes.pop(node_id, None)
            if node is None:
                continue
            bucket = self.by_module.get(node.module_id, [])
            if node_id in bucket:
                bucket.remove(node_id)
        subtree.mounted = False


@dataclass
class CompiledCapabilityIndex:
    # Search is intentionally fuzzy: aliases may collide and resolve to
    # multiple candidates. Execution must never use this index directly.
    records: dict[str, CapabilityDescriptor] = field(default_factory=dict)
    aliases: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    by_module: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    by_canonical: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def register(self, descriptor: CapabilityDescriptor) -> None:
        self.records[descriptor.name] = descriptor
        bucket = self.by_module[descriptor.module_id]
        if descriptor.name not in bucket:
            bucket.append(descriptor.name)
        canonical_bucket = self.by_canonical[descriptor.canonical_path or descriptor.name]
        if descriptor.name not in canonical_bucket:
            canonical_bucket.append(descriptor.name)
        for alias in (descriptor.display_name, *descriptor.aliases):
            if not alias:
                continue
            alias_bucket = self.aliases[alias]
            if descriptor.name not in alias_bucket:
                alias_bucket.append(descriptor.name)

    def unregister_many(self, record_ids: list[str]) -> None:
        for record_id in record_ids:
            descriptor = self.records.pop(record_id, None)
            if descriptor is None:
                continue
            bucket = self.by_module.get(descriptor.module_id, [])
            if record_id in bucket:
                bucket.remove(record_id)
            canonical_bucket = self.by_canonical.get(descriptor.canonical_path or descriptor.name, [])
            if record_id in canonical_bucket:
                canonical_bucket.remove(record_id)
            for alias in (descriptor.display_name, *descriptor.aliases):
                if not alias:
                    continue
                alias_bucket = self.aliases.get(alias, [])
                if record_id in alias_bucket:
                    alias_bucket.remove(record_id)


@dataclass
class BoundActionIndex:
    # This is the hot-path execution table. All routing resolves to a stable
    # (canonical_path, target_id) key and stays O(1).
    actions: dict[tuple[str, str], BoundCapabilityAction] = field(default_factory=dict)

    def register(self, action: BoundCapabilityAction) -> None:
        self.actions[(action.canonical_path, action.target_id)] = action

    def get(self, canonical_path: str, target_id: str) -> BoundCapabilityAction | None:
        return self.actions.get((canonical_path, target_id))

    def unregister_many(self, keys: list[tuple[str, str]]) -> None:
        for key in keys:
            self.actions.pop(key, None)


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
    args_schema: dict[str, Any] | None = None,
    result_schema: dict[str, Any] | None = None,
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
                family=family,
                description=description,
                aliases=tuple(aliases),
                args_schema=dict(args_schema or {}),
                result_schema=dict(result_schema or {}),
                metadata=dict(metadata or {}),
            )
        )
        fn.__capability_action_blueprints__ = existing
        return fn

    return decorator

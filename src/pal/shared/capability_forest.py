from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from pal.execution.contracts import CapabilityCall, CapabilityDescriptor, CapabilityResult
from pal.shared.tool_aliases import llm_tool_name


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
    # Descriptor records support search and rendering. Execution aliases are a
    # strict projection to canonical paths and may never be ambiguous.
    records: dict[str, CapabilityDescriptor] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    alias_records: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    record_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    explicit_alias_records: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    derived_alias_records: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    record_explicit_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    record_derived_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    by_module: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    by_canonical: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))

    def validate_register(self, descriptor: CapabilityDescriptor) -> None:
        existing_record = self.records.get(descriptor.name)
        if existing_record is not None and existing_record != descriptor:
            raise ValueError(f"capability descriptor name already registered: {descriptor.name}")
        canonical_path = str(descriptor.canonical_path or descriptor.name).strip()
        if not canonical_path:
            raise ValueError("capability canonical_path is required")
        existing_alias_target = self._alias_target(self.explicit_alias_records.get(canonical_path, []))
        if existing_alias_target is not None and existing_alias_target != canonical_path:
            raise ValueError(
                f"capability canonical path is already registered as an alias: "
                f"{canonical_path} -> {existing_alias_target}"
            )
        for alias in _descriptor_explicit_routing_aliases(descriptor):
            existing_canonical = self._alias_target(self.explicit_alias_records.get(alias, []))
            if existing_canonical is not None and existing_canonical != canonical_path:
                raise ValueError(
                    f"capability alias already routes to another canonical path: "
                    f"{alias} -> {existing_canonical}"
                )
            if self.by_canonical.get(alias) and alias != canonical_path:
                raise ValueError(f"capability alias conflicts with a canonical path: {alias}")

    def validate_register_many(self, descriptors: list[CapabilityDescriptor]) -> None:
        pending_records: dict[str, CapabilityDescriptor] = {}
        pending_aliases: dict[str, str] = {}
        pending_canonical_paths: set[str] = set()
        for descriptor in descriptors:
            self.validate_register(descriptor)
            previous = pending_records.get(descriptor.name)
            if previous is not None and previous != descriptor:
                raise ValueError(f"capability descriptor name already registered in batch: {descriptor.name}")
            pending_records[descriptor.name] = descriptor
            canonical_path = str(descriptor.canonical_path or descriptor.name).strip()
            previous_alias_target = pending_aliases.get(canonical_path)
            if previous_alias_target is not None and previous_alias_target != canonical_path:
                raise ValueError(
                    f"capability canonical path is already an alias in batch: "
                    f"{canonical_path} -> {previous_alias_target}"
                )
            pending_canonical_paths.add(canonical_path)
            for alias in _descriptor_explicit_routing_aliases(descriptor):
                if alias in pending_canonical_paths and alias != canonical_path:
                    raise ValueError(f"capability alias conflicts with a canonical path in batch: {alias}")
                previous_canonical = pending_aliases.get(alias)
                if previous_canonical is not None and previous_canonical != canonical_path:
                    raise ValueError(
                        f"capability alias maps to multiple canonical paths in batch: "
                        f"{alias} -> {previous_canonical}, {canonical_path}"
                    )
                pending_aliases[alias] = canonical_path
        for alias, canonical_path in pending_aliases.items():
            if alias in pending_canonical_paths and alias != canonical_path:
                raise ValueError(f"capability alias conflicts with a canonical path in batch: {alias}")

    def register(self, descriptor: CapabilityDescriptor) -> None:
        self.validate_register(descriptor)
        canonical_path = str(descriptor.canonical_path or descriptor.name).strip()
        if canonical_path in self.derived_alias_records:
            self._drop_derived_alias(canonical_path)
        self.records[descriptor.name] = descriptor
        bucket = self.by_module[descriptor.module_id]
        if descriptor.name not in bucket:
            bucket.append(descriptor.name)
        canonical_bucket = self.by_canonical[canonical_path]
        if descriptor.name not in canonical_bucket:
            canonical_bucket.append(descriptor.name)
        explicit_aliases = _descriptor_explicit_routing_aliases(descriptor)
        derived_aliases: list[str] = []
        for alias in explicit_aliases:
            self._register_alias_record(alias, descriptor.name, derived=False)
        for derived_alias in _descriptor_derived_routing_aliases(descriptor):
            if self.by_canonical.get(derived_alias):
                continue
            explicit_target = self._alias_target(self.explicit_alias_records.get(derived_alias, []))
            if explicit_target in {None, canonical_path}:
                self._register_alias_record(derived_alias, descriptor.name, derived=True)
                derived_aliases.append(derived_alias)
        self.record_explicit_aliases[descriptor.name] = explicit_aliases
        self.record_derived_aliases[descriptor.name] = tuple(derived_aliases)
        self.record_aliases[descriptor.name] = (*explicit_aliases, *derived_aliases)

    def unregister_many(self, record_ids: list[str]) -> None:
        for record_id in record_ids:
            descriptor = self.records.pop(record_id, None)
            if descriptor is None:
                continue
            bucket = self.by_module.get(descriptor.module_id, [])
            if record_id in bucket:
                bucket.remove(record_id)
            if not bucket:
                self.by_module.pop(descriptor.module_id, None)
            canonical_bucket = self.by_canonical.get(descriptor.canonical_path or descriptor.name, [])
            if record_id in canonical_bucket:
                canonical_bucket.remove(record_id)
            if not canonical_bucket:
                self.by_canonical.pop(descriptor.canonical_path or descriptor.name, None)
            explicit_aliases = self.record_explicit_aliases.pop(record_id, ())
            derived_aliases = self.record_derived_aliases.pop(record_id, ())
            self.record_aliases.pop(record_id, None)
            for alias in (*explicit_aliases, *derived_aliases):
                alias_bucket = self.alias_records.get(alias, [])
                if record_id in alias_bucket:
                    alias_bucket.remove(record_id)
                explicit_bucket = self.explicit_alias_records.get(alias, [])
                if record_id in explicit_bucket:
                    explicit_bucket.remove(record_id)
                derived_bucket = self.derived_alias_records.get(alias, [])
                if record_id in derived_bucket:
                    derived_bucket.remove(record_id)
                self._refresh_alias(alias)

    def canonical_path_for(self, name: str) -> str:
        normalized = str(name or "").strip()
        return self.aliases.get(normalized, normalized)

    def _register_alias_record(self, alias: str, record_id: str, *, derived: bool) -> None:
        bucket = self.alias_records[alias]
        if record_id not in bucket:
            bucket.append(record_id)
        source = self.derived_alias_records if derived else self.explicit_alias_records
        source_bucket = source[alias]
        if record_id not in source_bucket:
            source_bucket.append(record_id)
        self._refresh_alias(alias)

    def _drop_derived_alias(self, alias: str) -> None:
        record_ids = tuple(self.derived_alias_records.pop(alias, ()))
        for record_id in record_ids:
            aliases = tuple(item for item in self.record_derived_aliases.get(record_id, ()) if item != alias)
            self.record_derived_aliases[record_id] = aliases
            self.record_aliases[record_id] = tuple(
                item for item in self.record_aliases.get(record_id, ()) if item != alias
            )
            bucket = self.alias_records.get(alias, [])
            if record_id in bucket and record_id not in self.explicit_alias_records.get(alias, []):
                bucket.remove(record_id)
        self._refresh_alias(alias)

    def _refresh_alias(self, alias: str) -> None:
        explicit = [record_id for record_id in self.explicit_alias_records.get(alias, []) if record_id in self.records]
        derived = [record_id for record_id in self.derived_alias_records.get(alias, []) if record_id in self.records]
        self.explicit_alias_records[alias] = explicit
        self.derived_alias_records[alias] = derived
        combined = list(dict.fromkeys([*explicit, *derived]))
        if combined:
            self.alias_records[alias] = combined
        else:
            self.alias_records.pop(alias, None)
            self.explicit_alias_records.pop(alias, None)
            self.derived_alias_records.pop(alias, None)
            self.aliases.pop(alias, None)
            return
        target = self._alias_target(explicit) or self._alias_target(derived)
        if target is None:
            self.aliases.pop(alias, None)
        else:
            self.aliases[alias] = target

    def _alias_target(self, record_ids: list[str]) -> str | None:
        targets = {
            str(self.records[record_id].canonical_path or self.records[record_id].name).strip()
            for record_id in record_ids
            if record_id in self.records
        }
        if not targets:
            return None
        if len(targets) != 1:
            raise ValueError(f"capability alias maps to multiple canonical paths: {sorted(targets)}")
        return next(iter(targets))


@dataclass
class BoundActionIndex:
    # This is the hot-path execution table. All routing resolves to a stable
    # (canonical_path, target_id) key and stays O(1).
    actions: dict[tuple[str, str], BoundCapabilityAction] = field(default_factory=dict)

    def register(self, action: BoundCapabilityAction) -> None:
        key = (action.canonical_path, action.target_id)
        if key in self.actions:
            raise ValueError(f"canonical capability binding already registered: {key[0]} target={key[1]}")
        self.actions[key] = action

    def get(self, canonical_path: str, target_id: str) -> BoundCapabilityAction | None:
        return self.actions.get((canonical_path, target_id))

    def unregister_many(self, keys: list[tuple[str, str]]) -> None:
        for key in keys:
            self.actions.pop(key, None)


def _descriptor_explicit_routing_aliases(descriptor: CapabilityDescriptor) -> tuple[str, ...]:
    canonical_path = str(descriptor.canonical_path or descriptor.name).strip()
    descriptor_name = str(descriptor.name or "").strip()
    candidates = (descriptor_name, *descriptor.aliases)
    return tuple(
        dict.fromkeys(
            alias
            for value in candidates
            if (alias := str(value or "").strip()) and alias != canonical_path
        )
    )


def _descriptor_derived_routing_aliases(descriptor: CapabilityDescriptor) -> tuple[str, ...]:
    canonical_path = str(descriptor.canonical_path or descriptor.name).strip()
    candidates = [llm_tool_name(canonical_path)]
    if not descriptor.target_id or descriptor.target_id == SINGLETON_TARGET:
        return tuple(alias for alias in dict.fromkeys(candidates) if alias and alias != canonical_path)
    descriptor_name = str(descriptor.name or "").strip()
    instance_base_name, separator, _ = descriptor_name.rpartition("::")
    if separator:
        candidates.append(instance_base_name)
    return tuple(alias for alias in dict.fromkeys(candidates) if alias and alias != canonical_path)


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

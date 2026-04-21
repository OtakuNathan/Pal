from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

from pal.execution.contracts import CapabilityCall, CapabilityDescriptor, CapabilityResult
from pal.shared import (
    BoundCapabilityAction,
    CapabilityActionBlueprint,
    CapabilityNodeBlueprint,
    HydratedCapabilityNode,
    MountedSubtreeHandle,
    SINGLETON_TARGET,
)

_CANONICAL_NAMESPACE_ABBREVIATIONS = {
    "operation": "op",
    "introspection": "intro",
}

_CANONICAL_MODULE_ABBREVIATIONS = {
    "execution": "exec",
}

_CANONICAL_FAMILY_ABBREVIATIONS = {
    "management": "mgmt",
    "discovery": "disc",
}


@dataclass(frozen=True)
class HydrationTarget:
    target_id: str
    target_label: str
    payload: Any | None = None


def compile_provider_subtree(provider: Any, *, module_id: str, lifecycle_scope: str, detachable: bool) -> MountedSubtreeHandle:
    # Compilation happens in two phases:
    # 1. Read static blueprints from the provider class.
    # 2. Hydrate those blueprints against the runtime instance to produce the
    #    concrete subtree that Execution can mount.
    node_blueprints: list[CapabilityNodeBlueprint] = list(getattr(provider.__class__, "__capability_node_blueprints__", ()))
    action_blueprints = _collect_action_blueprints(provider)
    subtree = MountedSubtreeHandle(module_id=module_id)

    for node_blueprint in node_blueprints:
        targets = list(_hydrate_targets(provider, node_blueprint, module_id=module_id))
        for target in targets:
            node_id = f"{node_blueprint.namespace}:{node_blueprint.scope}:{module_id}:{target.target_id}"
            node = HydratedCapabilityNode(
                node_id=node_id,
                namespace=node_blueprint.namespace,
                scope=node_blueprint.scope,
                kind=node_blueprint.kind,
                module_id=module_id,
                source=node_blueprint.source,
                target_kind=node_blueprint.target_kind,
                target_id=target.target_id,
                target_label=target.target_label,
                metadata=dict(node_blueprint.metadata),
            )
            subtree.nodes.append(node)
            subtree.node_ids.append(node_id)

            for action_blueprint in action_blueprints:
                if action_blueprint.namespace != node_blueprint.namespace or action_blueprint.scope != node_blueprint.scope:
                    continue
                canonical_path = _canonical_path(
                    module_id=node_blueprint.path_module_id or module_id,
                    action_blueprint=action_blueprint,
                    node_blueprint=node_blueprint,
                )
                descriptor_name = canonical_path if target.target_id == SINGLETON_TARGET else f"{canonical_path}::{target.target_id}"
                descriptor = CapabilityDescriptor(
                    name=descriptor_name,
                    canonical_path=canonical_path,
                    family=action_blueprint.family or action_blueprint.namespace,
                    description=action_blueprint.description or f"{action_blueprint.action_name} {module_id} {node_blueprint.scope}",
                    source=node_blueprint.source,
                    display_name=_display_name(canonical_path, node_blueprint, target),
                    aliases=_aliases(module_id, action_blueprint, node_blueprint, target),
                    target_kind=node.target_kind,
                    target_id=target.target_id,
                    target_label=target.target_label,
                    parameters_schema=_parameters_schema(action_blueprint, target),
                    result_schema=deepcopy(action_blueprint.result_schema),
                    metadata={
                        "namespace": action_blueprint.namespace,
                        "scope": node_blueprint.scope,
                        "action": action_blueprint.action_name,
                        **dict(action_blueprint.metadata),
                    },
                    lifecycle_scope=lifecycle_scope,
                    module_id=module_id,
                    detachable=detachable,
                )
                handler = getattr(provider, action_blueprint.handler_name)
                bound_action = BoundCapabilityAction(
                    canonical_path=canonical_path,
                    target_id=target.target_id,
                    descriptor=descriptor,
                    callable=_bind_callable(handler, target),
                )
                subtree.descriptors.append(descriptor)
                subtree.bound_actions.append(bound_action)
                subtree.bound_action_keys.append((canonical_path, target.target_id))
                subtree.search_record_ids.append(descriptor.name)
                node.bound_action_keys.append((canonical_path, target.target_id))
                node.search_record_ids.append(descriptor.name)

    return subtree


def _collect_action_blueprints(provider: Any) -> list[CapabilityActionBlueprint]:
    collected: list[CapabilityActionBlueprint] = []
    for attr_name in dir(provider.__class__):
        value = getattr(provider.__class__, attr_name, None)
        if value is None:
            continue
        for blueprint in getattr(value, "__capability_action_blueprints__", ()):
            collected.append(blueprint)
    return collected


def _hydrate_targets(provider: Any, blueprint: CapabilityNodeBlueprint, *, module_id: str) -> Iterable[HydrationTarget]:
    if blueprint.iterable_resolver is None:
        yield HydrationTarget(target_id=SINGLETON_TARGET, target_label=module_id, payload=None)
        return
    items = getattr(provider, blueprint.iterable_resolver)()
    for item in items:
        target_id = _resolve_target(provider, blueprint.target_id_resolver, item)
        target_label = _resolve_target(provider, blueprint.target_label_resolver, item) or target_id
        yield HydrationTarget(target_id=str(target_id), target_label=str(target_label), payload=item)


def _resolve_target(provider: Any, resolver_name: str | None, payload: Any) -> Any:
    if resolver_name is None:
        return payload
    resolver = getattr(provider, resolver_name)
    return resolver(payload)


def _canonical_path(
    *,
    module_id: str,
    action_blueprint: CapabilityActionBlueprint,
    node_blueprint: CapabilityNodeBlueprint,
) -> str:
    return _underscore_canonical_path(
        module_id=module_id,
        action_blueprint=action_blueprint,
        node_blueprint=node_blueprint,
    )


def _underscore_canonical_path(
    *,
    module_id: str,
    action_blueprint: CapabilityActionBlueprint,
    node_blueprint: CapabilityNodeBlueprint,
) -> str:
    namespace = _abbreviate_canonical_namespace(action_blueprint.namespace)
    canonical_module_id = _abbreviate_canonical_module(module_id)
    if action_blueprint.namespace == "introspection":
        if node_blueprint.scope == module_id:
            return f"{namespace}_{canonical_module_id}_{action_blueprint.action_name}"
        return f"{namespace}_{node_blueprint.scope}_{canonical_module_id}_{action_blueprint.action_name}"
    if bool(action_blueprint.metadata.get("omit_family_in_canonical")):
        return f"{namespace}_{canonical_module_id}_{action_blueprint.action_name}"
    family = _abbreviate_canonical_family(action_blueprint.family or "operation")
    if family == canonical_module_id:
        return f"{namespace}_{canonical_module_id}_{action_blueprint.action_name}"
    return f"{namespace}_{canonical_module_id}_{family}_{action_blueprint.action_name}"


def _display_name(canonical_path: str, node_blueprint: CapabilityNodeBlueprint, target: HydrationTarget) -> str:
    if target.target_id == SINGLETON_TARGET:
        return canonical_path
    path_parts = canonical_path.split("_")
    return "_".join([*path_parts[:-1], target.target_label, path_parts[-1]])


def _aliases(
    module_id: str,
    action_blueprint: CapabilityActionBlueprint,
    node_blueprint: CapabilityNodeBlueprint,
    target: HydrationTarget,
) -> tuple[str, ...]:
    aliases = list(action_blueprint.aliases)
    if action_blueprint.namespace == "introspection" and node_blueprint.scope == "module":
        aliases.append(f"{module_id}_introspection_{action_blueprint.action_name}")
    if action_blueprint.namespace == "operation":
        family = action_blueprint.family or "operation"
        aliases.append(f"{module_id}_{family}_{action_blueprint.action_name}")
    if target.target_id != SINGLETON_TARGET:
        aliases.extend(
            [
                f"{action_blueprint.action_name} {target.target_label}",
                f"{module_id} {target.target_label} {action_blueprint.action_name}",
            ]
        )
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def _abbreviate_canonical_namespace(value: str) -> str:
    return _CANONICAL_NAMESPACE_ABBREVIATIONS.get(value, value)


def _abbreviate_canonical_module(value: str) -> str:
    return _CANONICAL_MODULE_ABBREVIATIONS.get(value, value)


def _abbreviate_canonical_family(value: str) -> str:
    return _CANONICAL_FAMILY_ABBREVIATIONS.get(value, value)


def _parameters_schema(action_blueprint: CapabilityActionBlueprint, target: HydrationTarget) -> dict[str, Any]:
    schema = deepcopy(action_blueprint.args_schema) or {"type": "object", "properties": {}, "required": []}
    schema.setdefault("type", "object")
    properties = schema.setdefault("properties", {})
    required = list(schema.setdefault("required", []))
    if target.target_id != SINGLETON_TARGET:
        # Instance-level actions must always expose target_id to the LLM-facing
        # schema so execution never has to guess which leaf node was intended.
        properties["target_id"] = {
            "type": "string",
            "enum": [target.target_id],
            "description": f"Target identifier for {target.target_label}.",
        }
        if "target_id" not in required:
            required.append("target_id")
        schema["required"] = required
    return schema


def _bind_callable(handler, target: HydrationTarget):
    def bound(call: CapabilityCall) -> CapabilityResult:
        args = dict(call.args)
        if target.target_id != SINGLETON_TARGET:
            args.setdefault("target_id", target.target_id)
        bound_call = CapabilityCall(
            name=call.name,
            args=args,
            meta={**dict(call.meta), "resolved_target": target.payload, "resolved_target_id": target.target_id},
        )
        return handler(bound_call)

    return bound

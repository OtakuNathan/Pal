from __future__ import annotations

from dataclasses import dataclass, replace
import inspect
import re
from typing import Any, Iterable

from pydantic import Field, create_model

from pal.execution.contracts import CapabilityCall, CapabilityDescriptor, CapabilityResult
from pal.execution.tool_facade import (
    EffectKind,
    EffectOutcome,
    EffectReceipt,
    Idempotency,
    InvocationMode,
    PagingMode,
    RetryPolicy,
    ToolExecutionSemantics,
    model_validation_schema,
)
from pal.shared import (
    BoundCapabilityAction,
    CapabilityActionBlueprint,
    CapabilityNodeBlueprint,
    HydratedCapabilityNode,
    MountedSubtreeHandle,
    RuntimeStatus,
    SINGLETON_TARGET,
)

_TOOL_ALIAS_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")

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

_TARGET_ARGUMENT_NAMES = {
    "endpoint": "name",
    "proactive_task": "name",
    "provider": "name",
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
        hydrated_nodes: dict[str, HydratedCapabilityNode] = {}
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
            hydrated_nodes[target.target_id] = node

        if not targets:
            continue
        representative = targets[0]
        is_targeted = representative.target_id != SINGLETON_TARGET
        for action_blueprint in action_blueprints:
            if action_blueprint.namespace != node_blueprint.namespace or action_blueprint.scope != node_blueprint.scope:
                continue
            canonical_path = _canonical_path(
                module_id=node_blueprint.path_module_id or module_id,
                action_blueprint=action_blueprint,
                node_blueprint=node_blueprint,
            )
            public_alias = _public_alias(action_blueprint)
            input_model = _bound_input_model(
                action_blueprint,
                representative,
                public_alias,
                node_blueprint=node_blueprint,
                module_id=module_id,
            )
            descriptor = CapabilityDescriptor(
                name=public_alias,
                canonical_path=canonical_path,
                family=action_blueprint.family or action_blueprint.namespace,
                source=node_blueprint.source,
                display_name=public_alias,
                aliases=(public_alias,),
                target_kind=node_blueprint.target_kind,
                target_id=SINGLETON_TARGET,
                target_label=module_id,
                InputModel=input_model,
                OutputModel=action_blueprint.OutputModel,
                guidance=action_blueprint.guidance,
                execution=action_blueprint.execution or _default_execution(action_blueprint),
                examples=_bound_examples(
                    action_blueprint,
                    representative,
                    input_model=input_model,
                    node_blueprint=node_blueprint,
                ),
                metadata={
                    "namespace": action_blueprint.namespace,
                    "scope": node_blueprint.scope,
                    "action": action_blueprint.action_name,
                    **(
                        {
                            "target_argument": _target_argument_name(node_blueprint),
                            "target_discovery_alias": _target_discovery_alias(
                                node_blueprint,
                                module_id=module_id,
                            ),
                        }
                        if is_targeted
                        else {}
                    ),
                    **dict(action_blueprint.metadata),
                },
                lifecycle_scope=lifecycle_scope,
                module_id=module_id,
                detachable=detachable,
            )
            subtree.descriptors.append(descriptor)
            subtree.search_record_ids.append(descriptor.name)
            for target in targets:
                node = hydrated_nodes[target.target_id]
                handler = getattr(provider, action_blueprint.handler_name)
                async_handler = (
                    getattr(provider, action_blueprint.async_handler_name)
                    if action_blueprint.async_handler_name is not None
                    else None
                )
                bound_action = BoundCapabilityAction(
                    canonical_path=canonical_path,
                    target_id=target.target_id,
                    descriptor=descriptor,
                    callable=_bind_callable(handler, target, descriptor.execution),
                    async_callable=(
                        _bind_callable(async_handler, target, descriptor.execution)
                        if async_handler is not None
                        else None
                    ),
                )
                subtree.bound_actions.append(bound_action)
                subtree.bound_action_keys.append((canonical_path, target.target_id))
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
    override = str(action_blueprint.metadata.get("canonical_path") or "").strip()
    if override:
        return override
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
    if family in {"operation", canonical_module_id}:
        return f"{namespace}_{canonical_module_id}_{action_blueprint.action_name}"
    return f"{namespace}_{canonical_module_id}_{family}_{action_blueprint.action_name}"


def _public_alias(action_blueprint: CapabilityActionBlueprint) -> str:
    aliases = tuple(str(value or "").strip() for value in action_blueprint.aliases)
    if len(aliases) != 1 or not aliases[0]:
        raise ValueError(
            f"capability {action_blueprint.handler_name!r} must declare exactly one non-empty alias"
        )
    base_name = aliases[0]
    _validate_alias(base_name, capability=action_blueprint.handler_name)
    return base_name


def _target_argument_name(node_blueprint: CapabilityNodeBlueprint) -> str:
    return _TARGET_ARGUMENT_NAMES.get(node_blueprint.target_kind, "target_id")


def _validate_alias(alias: str, *, capability: str) -> None:
    if _TOOL_ALIAS_RE.fullmatch(alias) is None:
        raise ValueError(
            f"capability {capability!r} declares invalid alias {alias!r}; "
            "expected 1-64 characters from [A-Za-z0-9_-]"
        )
    if alias.startswith(("op_", "intro_")):
        raise ValueError(
            f"capability {capability!r} alias {alias!r} uses the reserved canonical-path namespace"
        )


def _abbreviate_canonical_namespace(value: str) -> str:
    return _CANONICAL_NAMESPACE_ABBREVIATIONS.get(value, value)


def _abbreviate_canonical_module(value: str) -> str:
    return _CANONICAL_MODULE_ABBREVIATIONS.get(value, value)


def _abbreviate_canonical_family(value: str) -> str:
    return _CANONICAL_FAMILY_ABBREVIATIONS.get(value, value)


def _bound_input_model(
    action_blueprint: CapabilityActionBlueprint,
    target: HydrationTarget,
    descriptor_name: str,
    *,
    node_blueprint: CapabilityNodeBlueprint,
    module_id: str,
):
    if target.target_id == SINGLETON_TARGET:
        return action_blueprint.InputModel
    target_argument = _target_argument_name(node_blueprint)
    discovery_alias = _target_discovery_alias(node_blueprint, module_id=module_id)
    discovery_hint = f" returned by {discovery_alias}" if discovery_alias else ""
    return create_model(
        f"{_model_name(descriptor_name)}Input",
        __base__=action_blueprint.InputModel,
        **{
            target_argument: (
                str,
                Field(
                    description=(
                        f"Unique {node_blueprint.target_kind.replace('_', ' ')} name{discovery_hint}."
                    )
                ),
            )
        },
    )


def _target_discovery_alias(node_blueprint: CapabilityNodeBlueprint, *, module_id: str) -> str:
    if node_blueprint.target_kind == "endpoint":
        return "channel_list"
    if node_blueprint.target_kind == "proactive_task":
        return "proactive_list"
    if node_blueprint.target_kind == "provider":
        return {
            "memory": "memory_list_providers",
            "web_search": "web_search_list_providers",
        }.get(node_blueprint.path_module_id or module_id, "")
    return ""


def _bound_examples(
    action_blueprint: CapabilityActionBlueprint,
    target: HydrationTarget,
    *,
    input_model: type[BaseModel],
    node_blueprint: CapabilityNodeBlueprint,
) -> tuple[dict[str, Any], ...]:
    examples = tuple(dict(item) for item in action_blueprint.examples)
    if target.target_id != SINGLETON_TARGET:
        target_argument = _target_argument_name(node_blueprint)
        examples = tuple(
            {**item, target_argument: target.target_id}
            for item in examples
        )
    if examples:
        return examples
    schema = model_validation_schema(input_model)
    if not schema.get("properties"):
        return ()
    from pal.execution.tool_registry import _example_from_schema

    example = _example_from_schema(schema)
    if target.target_id != SINGLETON_TARGET:
        example[_target_argument_name(node_blueprint)] = target.target_id
    return (example,)


def _default_execution(action_blueprint: CapabilityActionBlueprint) -> ToolExecutionSemantics:
    if action_blueprint.namespace != "introspection":
        raise TypeError(
            "operation capability "
            f"{action_blueprint.handler_name!r} requires explicit execution semantics"
        )
    return ToolExecutionSemantics(
        invocation_mode=InvocationMode.INDIRECT,
        effect_kind=EffectKind.LOCAL_READ,
        idempotency=Idempotency.IDEMPOTENT,
        retry_policy=RetryPolicy.AUTOMATIC,
        paging=PagingMode.SUPPORTED,
    )


def _model_name(value: str) -> str:
    return "".join(part[:1].upper() + part[1:] for part in re.split(r"[^A-Za-z0-9]+", value) if part) or "Bound"


def _bind_callable(
    handler: Any,
    target: HydrationTarget,
    execution: ToolExecutionSemantics,
):
    def bound(call: CapabilityCall) -> CapabilityResult:
        args = dict(call.args)
        if target.target_id != SINGLETON_TARGET:
            args.setdefault("target_id", target.target_id)
        bound_call = CapabilityCall(
            name=call.name,
            args=args,
            meta={**dict(call.meta), "resolved_target": target.payload, "resolved_target_id": target.target_id},
        )
        result = handler(bound_call)
        if inspect.isawaitable(result):
            async def finish() -> CapabilityResult:
                return _attach_effect_receipt(await result, execution)

            return finish()  # type: ignore[return-value]
        return _attach_effect_receipt(result, execution)

    return bound


def _attach_effect_receipt(
    result: CapabilityResult,
    execution: ToolExecutionSemantics,
) -> CapabilityResult:
    receipt = getattr(result, "effect_receipt", None)
    if execution.effect_kind is not EffectKind.NONE and result.status == RuntimeStatus.OK and receipt is None:
        receipt = EffectReceipt(
            outcome=EffectOutcome.APPLIED,
            receipt={"capability_handler_completed": True},
        )
    if isinstance(result, CapabilityResult):
        return replace(result, effect_receipt=receipt)
    return CapabilityResult(
        status=result.status,
        text=str(getattr(result, "text", "") or ""),
        structured=getattr(result, "structured", None),
        llm_text=str(getattr(result, "llm_text", "") or ""),
        effect_receipt=receipt,
    )

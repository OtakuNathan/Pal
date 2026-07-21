from __future__ import annotations

from dataclasses import dataclass, replace
import inspect
import re
from typing import Any, Iterable, Literal

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
    ToolGuidance,
)
from pal.shared import (
    BoundCapabilityAction,
    CapabilityActionBlueprint,
    CapabilityNodeBlueprint,
    HydratedCapabilityNode,
    MountedSubtreeHandle,
    RuntimeStatus,
    SINGLETON_TARGET,
    llm_tool_name,
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
                descriptor_name = _descriptor_name(
                    canonical_path=canonical_path,
                    module_id=node_blueprint.path_module_id or module_id,
                    action_blueprint=action_blueprint,
                    node_blueprint=node_blueprint,
                    target=target,
                )
                descriptor = CapabilityDescriptor(
                    name=descriptor_name,
                    canonical_path=canonical_path,
                    family=action_blueprint.family or action_blueprint.namespace,
                    description=action_blueprint.description or f"{action_blueprint.action_name} {module_id} {node_blueprint.scope}",
                    source=node_blueprint.source,
                    display_name=descriptor_name,
                    aliases=_aliases(module_id, action_blueprint, node_blueprint, target),
                    target_kind=node.target_kind,
                    target_id=target.target_id,
                    target_label=target.target_label,
                    InputModel=_bound_input_model(action_blueprint, target, descriptor_name),
                    OutputModel=action_blueprint.OutputModel,
                    guidance=action_blueprint.guidance or _default_guidance(action_blueprint, module_id),
                    execution=action_blueprint.execution or _default_execution(action_blueprint),
                    search_text=action_blueprint.search_text,
                    examples=_bound_examples(action_blueprint, target),
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
                    callable=_bind_callable(handler, target, descriptor.execution),
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


def _descriptor_name(
    *,
    canonical_path: str,
    module_id: str,
    action_blueprint: CapabilityActionBlueprint,
    node_blueprint: CapabilityNodeBlueprint,
    target: HydrationTarget,
) -> str:
    if target.target_id == SINGLETON_TARGET:
        return llm_tool_name(canonical_path)
    scope = str(node_blueprint.scope or "").strip()
    if scope and scope not in {"module", module_id}:
        base_name = f"{module_id}_{scope}_{action_blueprint.action_name}"
    else:
        base_name = llm_tool_name(canonical_path)
    target_alias = re.sub(r"[^A-Za-z0-9_-]+", "_", target.target_id).strip("_")
    if not target_alias:
        raise ValueError(f"target id cannot produce a tool alias: {target.target_id!r}")
    return f"{base_name}__{target_alias}"


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


def _bound_input_model(
    action_blueprint: CapabilityActionBlueprint,
    target: HydrationTarget,
    descriptor_name: str,
):
    if target.target_id == SINGLETON_TARGET:
        return action_blueprint.InputModel
    target_literal = Literal.__getitem__((target.target_id,))
    return create_model(
        f"{_model_name(descriptor_name)}Input",
        __base__=action_blueprint.InputModel,
        target_id=(
            target_literal,
            Field(description=f"Target identifier for {target.target_label}."),
        ),
    )


def _bound_examples(
    action_blueprint: CapabilityActionBlueprint,
    target: HydrationTarget,
) -> tuple[dict[str, Any], ...]:
    if target.target_id == SINGLETON_TARGET:
        return tuple(dict(item) for item in action_blueprint.examples)
    return tuple({**dict(item), "target_id": target.target_id} for item in action_blueprint.examples)


def _default_guidance(action_blueprint: CapabilityActionBlueprint, module_id: str) -> ToolGuidance:
    purpose = str(
        action_blueprint.description
        or f"{action_blueprint.action_name} {module_id} {action_blueprint.scope}"
    ).strip()
    return ToolGuidance(
        purpose=purpose,
        use_when=purpose,
        do_not_use_when="Do not use when another tool's stated purpose matches the task more precisely.",
        failure_next_steps="Correct invalid input; otherwise follow the returned recovery affordances before retrying.",
    )


def _default_execution(action_blueprint: CapabilityActionBlueprint) -> ToolExecutionSemantics:
    if action_blueprint.namespace == "introspection":
        effect = EffectKind.LOCAL_READ
        idempotency = Idempotency.IDEMPOTENT
        retry = RetryPolicy.AUTOMATIC
    else:
        effect = EffectKind.LOCAL_WRITE
        idempotency = Idempotency.NON_IDEMPOTENT
        retry = RetryPolicy.RECONCILE_FIRST
    return ToolExecutionSemantics(
        invocation_mode=InvocationMode.INDIRECT,
        effect_kind=effect,
        idempotency=idempotency,
        retry_policy=retry,
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

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any, Literal, Union

from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field, create_model

from pal.execution.contracts import CapabilityCall, CapabilityDescriptor, CapabilityResult
from pal.execution.tool_facade import (
    EffectKind,
    EmptyToolInput,
    Idempotency,
    InvocationMode,
    McpToolOutput,
    PagingMode,
    RetryPolicy,
    Tool,
    ToolExecutionSemantics,
    ToolGuidance,
    compile_tool_description,
)
from pal.shared.capability_forest import BoundCapabilityAction, MountedSubtreeHandle, SINGLETON_TARGET


_FIXED_DIRECT_ALIASES = {"search_tools", "read_tool", "call_tool", "read_tool_result"}

# Direct exposure is an explicit product decision.  Everything else defaults to
# indirect discovery/call, including every MCP tool.
_BUILTIN_DIRECT_CANONICAL_PATHS = {
    "op_tool_search",
    "op_tool_read",
    "op_tool_call",
    "op_tool_result_page",
    "op_exec_shell",
    "op_file_read",
    "op_file_edit",
    "op_file_write",
    "op_path_delete",
    "op_git",
    "op_behavior_advise",
    "op_behavior_save",
    "op_behavior_affordance_update",
    "op_behavior_affordance_delete",
    "op_memory_recall",
    "op_memory_write",
    "op_memory_update",
    "op_memory_delete",
    "op_channel_send_attachment",
    "op_web_search",
    "op_web_read",
}


class FrozenDict(dict):
    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("registry generation values are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


class FrozenList(list):
    def _immutable(self, *_args: Any, **_kwargs: Any) -> None:
        raise TypeError("registry generation values are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable


@dataclass(frozen=True)
class CompiledToolRecord:
    alias: str
    canonical_path: str
    target_id: str
    descriptor_name: str
    module_id: str
    family: str
    source: str
    search_text: str
    description: str
    guidance: ToolGuidance
    execution: ToolExecutionSemantics
    input_model: type[BaseModel] | None
    output_model: type[BaseModel] | None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    example: dict[str, Any] | None
    binding: BoundCapabilityAction
    facade_tool: Tool | None = None
    is_mcp: bool = False
    requires_effect_receipt: bool = False

    def compact_input_shape(self) -> dict[str, Any]:
        properties = self.input_schema.get("properties")
        required = self.input_schema.get("required")
        return {
            "type": self.input_schema.get("type", "object"),
            "properties": sorted(properties) if isinstance(properties, dict) else [],
            "required": list(required) if isinstance(required, list) else [],
        }


@dataclass(frozen=True)
class FrozenCapabilityForest:
    nodes: MappingProxyType
    by_module: MappingProxyType


@dataclass(frozen=True)
class FrozenHydratedCapabilityNode:
    node_id: str
    namespace: str
    scope: str
    kind: str
    module_id: str
    source: str
    target_kind: str
    target_id: str
    target_label: str
    metadata: Any
    children: tuple[str, ...]
    bound_action_keys: tuple[tuple[str, str], ...]
    search_record_ids: tuple[str, ...]


@dataclass(frozen=True)
class FrozenMountedSubtree:
    module_id: str
    nodes: tuple[FrozenHydratedCapabilityNode, ...]
    descriptors: tuple[CapabilityDescriptor, ...]
    bound_actions: tuple[BoundCapabilityAction, ...]
    node_ids: tuple[str, ...]
    bound_action_keys: tuple[tuple[str, str], ...]
    search_record_ids: tuple[str, ...]
    mounted: bool = True


@dataclass(frozen=True)
class FrozenCapabilityIndex:
    records: MappingProxyType
    aliases: MappingProxyType
    by_module: MappingProxyType
    by_canonical: MappingProxyType

    def canonical_path_for(self, alias: str) -> str:
        normalized = str(alias or "").strip()
        return str(self.aliases.get(normalized) or normalized)


@dataclass(frozen=True)
class FrozenBoundActionIndex:
    actions: MappingProxyType

    def get(self, canonical_path: str, target_id: str) -> BoundCapabilityAction | None:
        return self.actions.get((canonical_path, target_id))


@dataclass(frozen=True)
class FrozenCapabilityRegistry:
    descriptors: MappingProxyType
    by_module: MappingProxyType


@dataclass(frozen=True)
class ToolRegistryGeneration:
    generation_id: int
    generation_hash: str
    forest: FrozenCapabilityForest
    canonical_bindings: FrozenBoundActionIndex
    capability_index: FrozenCapabilityIndex
    capability_registry: FrozenCapabilityRegistry
    direct_aliases: MappingProxyType
    indirect_aliases: MappingProxyType
    search_records: MappingProxyType
    provider_specs: MappingProxyType
    mounted_subtrees: MappingProxyType
    tool_implementations: MappingProxyType

    @classmethod
    def empty(cls) -> "ToolRegistryGeneration":
        return compile_registry_generation(
            generation_id=0,
            mounted_subtrees={},
            tool_implementations={},
        )

    def record_for_alias(self, alias: str) -> CompiledToolRecord | None:
        normalized = str(alias or "").strip()
        return self.direct_aliases.get(normalized) or self.indirect_aliases.get(normalized)


def compile_registry_generation(
    *,
    generation_id: int,
    mounted_subtrees: dict[str, MountedSubtreeHandle],
    tool_implementations: dict[str, Any],
) -> ToolRegistryGeneration:
    nodes: dict[str, Any] = {}
    forest_by_module: dict[str, list[str]] = {}
    descriptors: dict[str, CapabilityDescriptor] = {}
    descriptor_by_module: dict[str, list[str]] = {}
    by_canonical: dict[str, list[str]] = {}
    bindings: dict[tuple[str, str], BoundCapabilityAction] = {}

    for subtree_key in sorted(mounted_subtrees):
        subtree = mounted_subtrees[subtree_key]
        for node in subtree.nodes:
            if node.node_id in nodes:
                raise ValueError(f"capability node already mounted: {node.node_id}")
            nodes[node.node_id] = node
            forest_by_module.setdefault(node.module_id, []).append(node.node_id)
        for descriptor in subtree.descriptors:
            existing = descriptors.get(descriptor.name)
            if existing is not None and existing != descriptor:
                raise ValueError(f"capability descriptor name already registered: {descriptor.name}")
            descriptors[descriptor.name] = descriptor
            descriptor_by_module.setdefault(descriptor.module_id, []).append(descriptor.name)
            canonical_path = str(descriptor.canonical_path or descriptor.name).strip()
            by_canonical.setdefault(canonical_path, []).append(descriptor.name)
        for action in subtree.bound_actions:
            key = (str(action.canonical_path).strip(), str(action.target_id or SINGLETON_TARGET))
            if key in bindings:
                raise ValueError(f"canonical capability binding already registered: {key[0]} target={key[1]}")
            bindings[key] = action

    direct_aliases: dict[str, CompiledToolRecord] = {}
    indirect_aliases: dict[str, CompiledToolRecord] = {}
    alias_to_canonical: dict[str, str] = {}
    search_records: dict[str, dict[str, Any]] = {}
    provider_specs: dict[str, dict[str, Any]] = {}

    for descriptor_name in sorted(descriptors):
        descriptor = descriptors[descriptor_name]
        canonical_path = str(descriptor.canonical_path or descriptor.name).strip()
        target_id = str(descriptor.target_id or SINGLETON_TARGET)
        binding = bindings.get((canonical_path, target_id))
        if binding is None:
            raise ValueError(f"missing canonical binding: {canonical_path} target={target_id}")
        implementation = tool_implementations.get(canonical_path)
        record = _compile_record(descriptor, binding, implementation)
        alias = record.alias
        if alias in alias_to_canonical:
            raise ValueError(
                f"tool alias conflict in generation: {alias} -> "
                f"{alias_to_canonical[alias]}, {canonical_path}"
            )
        if alias == canonical_path:
            raise ValueError(f"tool alias exposes canonical path: {alias}")
        alias_to_canonical[alias] = canonical_path
        target = direct_aliases if record.execution.invocation_mode is InvocationMode.DIRECT else indirect_aliases
        target[alias] = record
        search_records[alias] = {
            "alias": alias,
            "search_text": record.search_text,
            "invocation_mode": record.execution.invocation_mode.value,
            "input_shape": record.compact_input_shape(),
        }
        if record.execution.invocation_mode is InvocationMode.DIRECT:
            provider_specs[alias] = {
                "type": "function",
                "function": {
                    "name": alias,
                    "description": record.description,
                    "input_schema": record.input_schema,
                },
            }

    # Descriptors are compiled before the generation-wide alias table exists.
    # Once it is complete, remove every canonical path from all LLM-facing
    # prose and schema descriptions in one deterministic projection pass.
    translations = sorted(alias_to_canonical.items(), key=lambda item: len(item[1]), reverse=True)
    for table in (direct_aliases, indirect_aliases):
        for alias, record in tuple(table.items()):
            table[alias] = replace(
                record,
                description=_project_llm_text(record.description, translations),
                search_text=_project_llm_text(record.search_text, translations),
                input_schema=_deep_freeze(_project_llm_value(record.input_schema, translations)),
                output_schema=_deep_freeze(_project_llm_value(record.output_schema, translations)),
            )
    search_records.clear()
    provider_specs.clear()
    for alias in sorted(alias_to_canonical):
        record = direct_aliases.get(alias) or indirect_aliases[alias]
        search_records[alias] = {
            "alias": alias,
            "search_text": record.search_text,
            "invocation_mode": record.execution.invocation_mode.value,
            "input_shape": record.compact_input_shape(),
        }
        if record.execution.invocation_mode is InvocationMode.DIRECT:
            provider_specs[alias] = {
                "type": "function",
                "function": {
                    "name": alias,
                    "description": record.description,
                    "input_schema": record.input_schema,
                },
            }

    stable = {
        "aliases": [
            {
                "alias": alias,
                "canonical_path": alias_to_canonical[alias],
                "mode": (direct_aliases.get(alias) or indirect_aliases[alias]).execution.invocation_mode.value,
                "input": (direct_aliases.get(alias) or indirect_aliases[alias]).input_schema,
                "output": (direct_aliases.get(alias) or indirect_aliases[alias]).output_schema,
                "description": (direct_aliases.get(alias) or indirect_aliases[alias]).description,
            }
            for alias in sorted(alias_to_canonical)
        ]
    }
    generation_hash = hashlib.sha256(
        json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    immutable_descriptors = {
        name: _freeze_descriptor(descriptor)
        for name, descriptor in descriptors.items()
    }
    immutable_nodes = {
        node_id: _freeze_node(node)
        for node_id, node in nodes.items()
    }
    immutable_bindings = {
        key: replace(action, descriptor=immutable_descriptors[action.descriptor.name])
        for key, action in bindings.items()
    }
    direct_aliases = {
        alias: _freeze_record(record, immutable_descriptors)
        for alias, record in direct_aliases.items()
    }
    indirect_aliases = {
        alias: _freeze_record(record, immutable_descriptors)
        for alias, record in indirect_aliases.items()
    }
    immutable_subtrees = {
        key: _freeze_subtree(
            subtree,
            nodes=immutable_nodes,
            descriptors=immutable_descriptors,
            bindings=immutable_bindings,
        )
        for key, subtree in mounted_subtrees.items()
    }
    frozen_records = _freeze_mapping(immutable_descriptors)
    frozen_by_module = _freeze_sequence_mapping(descriptor_by_module)
    return ToolRegistryGeneration(
        generation_id=generation_id,
        generation_hash=generation_hash,
        forest=FrozenCapabilityForest(
            nodes=_freeze_mapping(immutable_nodes),
            by_module=_freeze_sequence_mapping(forest_by_module),
        ),
        canonical_bindings=FrozenBoundActionIndex(actions=_freeze_mapping(immutable_bindings)),
        capability_index=FrozenCapabilityIndex(
            records=frozen_records,
            aliases=_freeze_mapping(alias_to_canonical),
            by_module=frozen_by_module,
            by_canonical=_freeze_sequence_mapping(by_canonical),
        ),
        capability_registry=FrozenCapabilityRegistry(
            descriptors=frozen_records,
            by_module=frozen_by_module,
        ),
        direct_aliases=_freeze_mapping(direct_aliases),
        indirect_aliases=_freeze_mapping(indirect_aliases),
        search_records=_freeze_mapping(search_records),
        provider_specs=_freeze_mapping(provider_specs),
        mounted_subtrees=_freeze_mapping(immutable_subtrees),
        tool_implementations=_freeze_mapping(tool_implementations),
    )


def subtree_for_tool(tool: Tool) -> MountedSubtreeHandle:
    descriptor = CapabilityDescriptor(
        name=tool.alias,
        canonical_path=tool.canonical_path,
        family=tool.family,
        description=tool.guidance.purpose,
        source=tool.source,
        display_name=tool.alias,
        target_id=tool.target_id,
        parameters_schema=tool.InputModel.model_json_schema(mode="validation"),
        result_schema=tool.OutputModel.model_json_schema(mode="validation"),
        metadata={
            **dict(tool.metadata),
            "alias": tool.alias,
            "search_text": tool.search_text,
            "guidance": tool.guidance.model_dump(mode="json"),
            "execution_semantics": tool.execution.model_dump(mode="json"),
            "examples": [dict(item) for item in tool.examples],
            "facade_tool": True,
        },
        module_id=tool.module_id or "standalone_tools",
    )

    def call_facade(call: CapabilityCall) -> CapabilityResult:
        validated = tool.InputModel.model_validate(call.args, strict=True)
        return tool.handler(validated)

    action = BoundCapabilityAction(
        canonical_path=tool.canonical_path,
        target_id=tool.target_id,
        descriptor=descriptor,
        callable=call_facade,
    )
    subtree = MountedSubtreeHandle(module_id=descriptor.module_id)
    subtree.descriptors.append(descriptor)
    subtree.bound_actions.append(action)
    subtree.bound_action_keys.append((action.canonical_path, action.target_id))
    subtree.search_record_ids.append(descriptor.name)
    return subtree


def _compile_record(
    descriptor: CapabilityDescriptor,
    binding: BoundCapabilityAction,
    implementation: Any,
) -> CompiledToolRecord:
    metadata = dict(descriptor.metadata or {})
    facade_tool = implementation if isinstance(implementation, Tool) else None
    alias = str(
        metadata.get("alias")
        or (facade_tool.alias if facade_tool is not None else "")
        or descriptor.name
    ).strip()
    if not alias:
        raise ValueError(f"tool alias is required for {descriptor.canonical_path or descriptor.name}")
    if len(alias) > 64 or re.fullmatch(r"[A-Za-z0-9_-]+", alias) is None:
        raise ValueError(f"invalid tool alias {alias!r}; expected 1-64 characters from [A-Za-z0-9_-]")
    if alias.startswith(("op_", "intro_")):
        raise ValueError(f"tool alias {alias!r} uses the reserved canonical-path namespace")
    canonical_path = str(descriptor.canonical_path or descriptor.name).strip()
    is_mcp = isinstance(metadata.get("mcp"), dict) and metadata["mcp"].get("kind") == "tool"

    if facade_tool is not None:
        input_model = facade_tool.InputModel
        output_model = facade_tool.OutputModel
        input_schema = input_model.model_json_schema(mode="validation")
        output_schema = output_model.model_json_schema(mode="validation")
        examples = tuple(_deep_thaw(item) for item in facade_tool.examples)
        guidance = facade_tool.guidance
        execution = facade_tool.execution
        search_text = facade_tool.search_text
    elif is_mcp:
        input_model = None
        output_model = None
        input_schema = dict(descriptor.parameters_schema or {"type": "object", "properties": {}})
        output_schema = dict(descriptor.result_schema or McpToolOutput.model_json_schema(mode="validation"))
        Draft202012Validator.check_schema(input_schema)
        Draft202012Validator.check_schema(output_schema)
        examples = ()
        guidance = _guidance_from_descriptor(descriptor)
        execution = _execution_from_descriptor(descriptor, alias=alias, is_mcp=True)
        search_text = str(metadata.get("search_text") or _default_search_text(descriptor, alias)).strip()
    else:
        input_schema_source = _tool_schema(implementation, "args_schema") or descriptor.parameters_schema
        output_schema_source = _tool_schema(implementation, "result_schema") or descriptor.result_schema
        input_model = model_from_json_schema(
            f"{_model_name(alias)}Input",
            dict(input_schema_source or {"type": "object", "properties": {}}),
            input_contract=True,
        )
        output_model = model_from_json_schema(
            f"{_model_name(alias)}Output",
            dict(output_schema_source or {"type": "object", "properties": {}}),
            input_contract=False,
        )
        input_schema = input_model.model_json_schema(mode="validation")
        output_schema = output_model.model_json_schema(mode="validation")
        configured_examples = metadata.get("examples") or getattr(implementation, "examples", ()) or ()
        examples = tuple(dict(item) for item in configured_examples if isinstance(item, dict))
        if input_schema.get("properties") and not examples:
            examples = (_example_from_schema(input_schema),)
        guidance = _guidance_from_descriptor(descriptor)
        execution = _execution_from_descriptor(descriptor, alias=alias, is_mcp=False)
        search_text = str(metadata.get("search_text") or _default_search_text(descriptor, alias)).strip()

    example = dict(examples[0]) if examples else None
    for candidate in examples:
        if input_model is not None:
            input_model.model_validate(candidate, strict=True)
        else:
            Draft202012Validator(input_schema).validate(candidate)
    description = compile_tool_description(
        alias=alias,
        guidance=guidance,
        execution=execution,
        input_schema=input_schema,
        output_schema=output_schema,
        example=example,
    )
    return CompiledToolRecord(
        alias=alias,
        canonical_path=canonical_path,
        target_id=str(descriptor.target_id or SINGLETON_TARGET),
        descriptor_name=descriptor.name,
        module_id=descriptor.module_id,
        family=descriptor.family,
        source=descriptor.source,
        search_text=search_text,
        description=description,
        guidance=guidance,
        execution=execution,
        input_model=input_model,
        output_model=output_model,
        input_schema=input_schema,
        output_schema=output_schema,
        example=example,
        binding=binding,
        facade_tool=facade_tool,
        is_mcp=is_mcp,
        requires_effect_receipt=facade_tool is not None and execution.effect_kind is not EffectKind.NONE,
    )


def model_from_json_schema(
    name: str,
    schema: dict[str, Any],
    *,
    input_contract: bool,
) -> type[BaseModel]:
    Draft202012Validator.check_schema(schema)
    if schema.get("type") not in {None, "object"}:
        raise ValueError(f"Pal tool {name} schema must have object root")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    required = set(schema.get("required") or ())
    fields: dict[str, Any] = {}
    for field_name, field_schema in properties.items():
        normalized = field_schema if isinstance(field_schema, dict) else {}
        annotation = _annotation_from_schema(f"{name}{_model_name(field_name)}", normalized)
        default: Any
        if field_name in required:
            default = ...
        elif "default" in normalized:
            default = normalized["default"]
        else:
            default = None
        constraints = _field_constraints(normalized)
        fields[str(field_name)] = (annotation, Field(default, **constraints))
    extra = "forbid" if input_contract or schema.get("additionalProperties") is False else "allow"
    return create_model(
        name,
        __config__=ConfigDict(strict=True, extra=extra),
        **fields,
    )


def _annotation_from_schema(name: str, schema: dict[str, Any]) -> Any:
    if "const" in schema:
        return Literal.__getitem__((schema["const"],))
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return Literal.__getitem__(tuple(enum))
    variants = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(variants, list) and variants:
        annotations = tuple(
            _annotation_from_schema(f"{name}Variant{index}", item if isinstance(item, dict) else {})
            for index, item in enumerate(variants)
        )
        return Union[annotations]  # type: ignore[arg-type]
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        annotations = tuple(_annotation_from_schema(name, {**schema, "type": item}) for item in schema_type)
        return Union[annotations]  # type: ignore[arg-type]
    if schema_type == "string":
        return str
    if schema_type == "integer":
        return int
    if schema_type == "number":
        return float
    if schema_type == "boolean":
        return bool
    if schema_type == "null":
        return type(None)
    if schema_type == "array":
        item_schema = schema.get("items") if isinstance(schema.get("items"), dict) else {}
        return list[_annotation_from_schema(f"{name}Item", item_schema)]
    if schema_type == "object" or isinstance(schema.get("properties"), dict):
        properties = schema.get("properties")
        if not properties and schema.get("additionalProperties") is not False:
            additional = schema.get("additionalProperties")
            item_type = _annotation_from_schema(f"{name}Value", additional) if isinstance(additional, dict) else Any
            return dict[str, item_type]
        return model_from_json_schema(name, schema, input_contract=schema.get("additionalProperties") is False)
    return Any


def _field_constraints(schema: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "minimum": "ge",
        "maximum": "le",
        "exclusiveMinimum": "gt",
        "exclusiveMaximum": "lt",
        "minLength": "min_length",
        "maxLength": "max_length",
        "pattern": "pattern",
        "minItems": "min_length",
        "maxItems": "max_length",
        "multipleOf": "multiple_of",
        "description": "description",
    }
    return {target: schema[source] for source, target in mapping.items() if source in schema}


def _guidance_from_descriptor(descriptor: CapabilityDescriptor) -> ToolGuidance:
    raw = descriptor.metadata.get("guidance") if isinstance(descriptor.metadata, dict) else None
    if isinstance(raw, ToolGuidance):
        return raw
    if isinstance(raw, dict):
        return ToolGuidance.model_validate(raw)
    purpose = str(descriptor.description or descriptor.display_name or descriptor.name).strip()
    return ToolGuidance(
        purpose=purpose,
        use_when=purpose,
        do_not_use_when="Do not use when another tool's stated purpose matches the task more precisely.",
        failure_next_steps="Correct invalid input; otherwise inspect the returned recovery affordances before retrying.",
    )


def _execution_from_descriptor(
    descriptor: CapabilityDescriptor,
    *,
    alias: str,
    is_mcp: bool,
) -> ToolExecutionSemantics:
    raw = descriptor.metadata.get("execution_semantics") if isinstance(descriptor.metadata, dict) else None
    if isinstance(raw, ToolExecutionSemantics):
        return raw
    if isinstance(raw, dict):
        # Metadata is a serialized registry source, not a tool invocation.
        # Enum values therefore arrive as JSON strings and are decoded before
        # the strict runtime contract is used.
        return ToolExecutionSemantics.model_validate(raw, strict=False)
    canonical = str(descriptor.canonical_path or descriptor.name)
    mode = InvocationMode.DIRECT if alias in _FIXED_DIRECT_ALIASES or canonical in _BUILTIN_DIRECT_CANONICAL_PATHS else InvocationMode.INDIRECT
    if is_mcp:
        annotations = dict(dict(descriptor.metadata.get("mcp") or {}).get("annotations") or {})
        declared = str(annotations.get("invocation_mode") or descriptor.metadata.get("invocation_mode") or "").strip()
        mode = InvocationMode.DIRECT if declared == InvocationMode.DIRECT.value else InvocationMode.INDIRECT
        read_only = bool(annotations.get("readOnlyHint"))
        effect = EffectKind.EXTERNAL_READ if read_only else EffectKind.EXTERNAL_WRITE
        idempotency = Idempotency.IDEMPOTENT if read_only or bool(annotations.get("idempotentHint")) else Idempotency.NON_IDEMPOTENT
        retry = RetryPolicy.AUTOMATIC if read_only else RetryPolicy.RECONCILE_FIRST
        return ToolExecutionSemantics(
            invocation_mode=mode,
            effect_kind=effect,
            idempotency=idempotency,
            retry_policy=retry,
            paging=PagingMode.SUPPORTED,
        )
    effect = _infer_effect_kind(canonical)
    non_idempotent = effect in {EffectKind.EXTERNAL_WRITE, EffectKind.CONTROL}
    return ToolExecutionSemantics(
        invocation_mode=mode,
        effect_kind=effect,
        idempotency=Idempotency.NON_IDEMPOTENT if non_idempotent else Idempotency.IDEMPOTENT,
        retry_policy=RetryPolicy.RECONCILE_FIRST if non_idempotent else RetryPolicy.AUTOMATIC,
        paging=PagingMode.SUPPORTED,
    )


def _infer_effect_kind(canonical: str) -> EffectKind:
    lowered = canonical.lower()
    if canonical in {"op_tool_search", "op_tool_read", "op_tool_result_page"} or any(
        token in lowered for token in ("_show", "_list", "_read", "_search", "_status", "_inspect", "_health")
    ):
        return EffectKind.LOCAL_READ
    if canonical in {"op_web_search", "op_web_read", "op_memory_recall"}:
        return EffectKind.EXTERNAL_READ
    if canonical in {"op_channel_send_attachment"}:
        return EffectKind.EXTERNAL_WRITE
    if any(token in lowered for token in ("_attach", "_detach", "_enable", "_disable", "_restart", "_cancel")):
        return EffectKind.CONTROL
    if any(token in lowered for token in ("_write", "_edit", "_delete", "_update", "_save", "_set_", "_create")):
        return EffectKind.LOCAL_WRITE
    return EffectKind.NONE


def _default_search_text(descriptor: CapabilityDescriptor, alias: str) -> str:
    return " ".join(
        value
        for value in (
            alias,
            descriptor.description,
            descriptor.family,
            descriptor.module_id,
            descriptor.target_label,
        )
        if str(value or "").strip()
    )


def _tool_schema(implementation: Any, attribute: str) -> dict[str, Any]:
    value = getattr(implementation, attribute, None) if implementation is not None else None
    return dict(value) if isinstance(value, dict) else {}


def _example_from_schema(
    schema: dict[str, Any],
    *,
    root_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    root = root_schema or schema
    schema = _resolve_example_schema(schema, root)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    required = set(schema.get("required") or ())
    example: dict[str, Any] = {}
    for name, item in properties.items():
        if name not in required:
            continue
        example[name] = _example_value(
            item if isinstance(item, dict) else {},
            root_schema=root,
        )
    return example


def _example_value(schema: dict[str, Any], *, root_schema: dict[str, Any]) -> Any:
    schema = _resolve_example_schema(schema, root_schema)
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    if "default" in schema:
        return schema["default"]
    variants = schema.get("oneOf") or schema.get("anyOf")
    if isinstance(variants, list) and variants:
        return _example_value(
            variants[0] if isinstance(variants[0], dict) else {},
            root_schema=root_schema,
        )
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        schema_type = next((item for item in schema_type if item != "null"), "null")
    if schema_type == "string":
        return "example"
    if schema_type == "integer":
        return max(1, int(schema.get("minimum") or 1))
    if schema_type == "number":
        return max(1.0, float(schema.get("minimum") or 1.0))
    if schema_type == "boolean":
        return False
    if schema_type == "array":
        return (
            []
            if int(schema.get("minItems") or 0) == 0
            else [
                _example_value(
                    dict(schema.get("items") or {}),
                    root_schema=root_schema,
                )
            ]
        )
    if schema_type == "object" or "properties" in schema:
        return _example_from_schema(schema, root_schema=root_schema)
    return None


def _resolve_example_schema(schema: dict[str, Any], root_schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve the local references emitted by Pydantic's generated schema.

    Examples are generated only after dynamic binding.  At that point nested
    models are represented through ``$defs``/``$ref``; treating a reference as
    an unknown value produced ``None`` for required object fields and made an
    otherwise valid registry generation fail compilation.
    """

    resolved = dict(schema)
    seen: set[str] = set()
    while isinstance(resolved.get("$ref"), str):
        reference = str(resolved["$ref"])
        if reference in seen or not reference.startswith("#/"):
            break
        seen.add(reference)
        target: Any = root_schema
        for raw_part in reference[2:].split("/"):
            part = raw_part.replace("~1", "/").replace("~0", "~")
            if not isinstance(target, dict) or part not in target:
                target = None
                break
            target = target[part]
        if not isinstance(target, dict):
            break
        overlay = {key: value for key, value in resolved.items() if key != "$ref"}
        resolved = {**target, **overlay}
    all_of = resolved.get("allOf")
    if isinstance(all_of, list) and all_of:
        merged = {key: value for key, value in resolved.items() if key != "allOf"}
        for component in all_of:
            if isinstance(component, dict):
                merged.update(_resolve_example_schema(component, root_schema))
        resolved = merged
    return resolved


def _model_name(value: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", str(value or ""))
    return "".join(word[:1].upper() + word[1:] for word in words) or "Tool"


def _freeze_mapping(value: dict[Any, Any]) -> MappingProxyType:
    return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})


def _freeze_sequence_mapping(value: dict[str, list[str]]) -> MappingProxyType:
    return MappingProxyType({key: tuple(items) for key, items in value.items()})


def _freeze_descriptor(descriptor: CapabilityDescriptor) -> CapabilityDescriptor:
    return replace(
        descriptor,
        aliases=tuple(descriptor.aliases),
        parameters_schema=_deep_freeze(descriptor.parameters_schema),
        result_schema=_deep_freeze(descriptor.result_schema),
        metadata=_deep_freeze(descriptor.metadata),
    )


def _freeze_node(node: Any) -> FrozenHydratedCapabilityNode:
    return FrozenHydratedCapabilityNode(
        node_id=str(node.node_id),
        namespace=str(node.namespace),
        scope=str(node.scope),
        kind=str(node.kind),
        module_id=str(node.module_id),
        source=str(node.source),
        target_kind=str(node.target_kind),
        target_id=str(node.target_id),
        target_label=str(node.target_label),
        metadata=_deep_freeze(dict(node.metadata or {})),
        children=tuple(node.children or ()),
        bound_action_keys=tuple(tuple(item) for item in node.bound_action_keys or ()),
        search_record_ids=tuple(node.search_record_ids or ()),
    )


def _freeze_record(
    record: CompiledToolRecord,
    descriptors: dict[str, CapabilityDescriptor],
) -> CompiledToolRecord:
    return replace(
        record,
        binding=replace(
            record.binding,
            descriptor=descriptors[record.descriptor_name],
        ),
    )


def _freeze_subtree(
    subtree: Any,
    *,
    nodes: dict[str, FrozenHydratedCapabilityNode],
    descriptors: dict[str, CapabilityDescriptor],
    bindings: dict[tuple[str, str], BoundCapabilityAction],
) -> FrozenMountedSubtree:
    return FrozenMountedSubtree(
        module_id=str(subtree.module_id),
        nodes=tuple(nodes[str(node.node_id)] for node in subtree.nodes),
        descriptors=tuple(descriptors[str(descriptor.name)] for descriptor in subtree.descriptors),
        bound_actions=tuple(
            bindings[(str(action.canonical_path), str(action.target_id or SINGLETON_TARGET))]
            for action in subtree.bound_actions
        ),
        node_ids=tuple(str(item) for item in subtree.node_ids),
        bound_action_keys=tuple((str(path), str(target)) for path, target in subtree.bound_action_keys),
        search_record_ids=tuple(str(item) for item in subtree.search_record_ids),
    )


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, FrozenDict | FrozenList):
        return value
    if isinstance(value, dict):
        return FrozenDict({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(_deep_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_deep_freeze(item) for item in value)
    return value


def _deep_thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _deep_thaw(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_deep_thaw(item) for item in value]
    if isinstance(value, set | frozenset):
        return [_deep_thaw(item) for item in value]
    return value


def _project_llm_text(value: str, translations: list[tuple[str, str]]) -> str:
    rendered = str(value or "")
    for alias, canonical in translations:
        rendered = rendered.replace(canonical, alias)
    rendered = re.sub(
        r"\b(?:op|intro)_[A-Za-z0-9_]+\b",
        lambda match: match.group(0).split("_", 1)[1],
        rendered,
    )
    return rendered


def _project_llm_value(value: Any, translations: list[tuple[str, str]]) -> Any:
    if isinstance(value, str):
        return _project_llm_text(value, translations)
    if isinstance(value, dict):
        return {key: _project_llm_value(item, translations) for key, item in value.items()}
    if isinstance(value, list):
        return [_project_llm_value(item, translations) for item in value]
    return value


__all__ = [
    "CompiledToolRecord",
    "FrozenBoundActionIndex",
    "FrozenCapabilityForest",
    "FrozenCapabilityIndex",
    "FrozenCapabilityRegistry",
    "ToolRegistryGeneration",
    "compile_registry_generation",
    "model_from_json_schema",
    "subtree_for_tool",
]

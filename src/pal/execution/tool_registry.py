from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator
from pydantic import BaseModel

from pal.execution.contracts import CapabilityCall, CapabilityDescriptor, CapabilityResult
from pal.execution.tool_facade import (
    EffectKind,
    InvocationMode,
    McpToolOutput,
    ToolExecutionSemantics,
    ToolGuidance,
    compile_tool_description,
    model_validation_schema,
)
from pal.shared.capability_forest import BoundCapabilityAction, MountedSubtreeHandle, SINGLETON_TARGET


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
    namespace: str
    tags: tuple[str, ...]
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

    @classmethod
    def empty(cls) -> "ToolRegistryGeneration":
        return compile_registry_generation(
            generation_id=0,
            mounted_subtrees={},
        )

    def record_for_alias(self, alias: str) -> CompiledToolRecord | None:
        normalized = str(alias or "").strip()
        return self.direct_aliases.get(normalized) or self.indirect_aliases.get(normalized)

    def project_llm_text(self, value: object) -> str:
        translations = _unambiguous_alias_translations(self.capability_index.aliases)
        return _project_llm_text(str(value or ""), translations, unknown="redact")

    def project_llm_value(self, value: Any) -> Any:
        translations = _unambiguous_alias_translations(self.capability_index.aliases)
        return _project_llm_value(value, translations, unknown="redact")


def compile_registry_generation(
    *,
    generation_id: int,
    mounted_subtrees: dict[str, MountedSubtreeHandle],
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
                shared_aliases = set(existing.aliases) & set(descriptor.aliases)
                same_targeted_action = (
                    shared_aliases
                    and existing.canonical_path == descriptor.canonical_path
                    and existing.metadata.get("target_argument") == descriptor.metadata.get("target_argument")
                    and bool(existing.metadata.get("target_argument"))
                )
                if same_targeted_action:
                    _assert_compatible_target_descriptors(existing, descriptor)
                    descriptor_names = descriptor_by_module.setdefault(descriptor.module_id, [])
                    if descriptor.name not in descriptor_names:
                        descriptor_names.append(descriptor.name)
                    continue
                if shared_aliases:
                    alias = sorted(shared_aliases)[0]
                    raise ValueError(
                        f"tool alias conflict in generation: {alias} -> "
                        f"{existing.canonical_path}, {descriptor.canonical_path}"
                    )
                raise ValueError(
                    f"capability descriptor name already registered: {descriptor.name}"
                )
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
        if binding is None and descriptor.metadata.get("target_argument"):
            binding = next(
                (
                    candidate
                    for (candidate_path, _candidate_target), candidate in sorted(bindings.items())
                    if candidate_path == canonical_path
                ),
                None,
            )
        if binding is None:
            raise ValueError(f"missing canonical binding: {canonical_path} target={target_id}")
        record = _compile_record(descriptor, binding)
        alias = record.alias
        if alias in alias_to_canonical:
            if alias_to_canonical[alias] != canonical_path:
                raise ValueError(
                    f"tool alias conflict in generation: {alias} -> "
                    f"{alias_to_canonical[alias]}, {canonical_path}"
                )
            existing = direct_aliases.get(alias) or indirect_aliases.get(alias)
            if existing is None:
                raise ValueError(f"tool alias {alias!r} has no compiled record")
            _assert_compatible_target_records(existing, record)
            continue
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
            "namespace": record.namespace,
            "family": record.family,
            "module_id": record.module_id,
            "tags": list(record.tags),
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
    for table in (direct_aliases, indirect_aliases):
        for alias, record in tuple(table.items()):
            translations = _unambiguous_alias_translations(
                alias_to_canonical,
                preferred=(record.alias, record.canonical_path),
            )
            unknown = "preserve" if record.is_mcp else "raise"
            table[alias] = replace(
                record,
                description=_project_llm_text(record.description, translations, unknown=unknown),
                search_text=_project_llm_text(record.search_text, translations, unknown=unknown),
                input_schema=_deep_freeze(_project_llm_value(record.input_schema, translations, unknown=unknown)),
                output_schema=_deep_freeze(_project_llm_value(record.output_schema, translations, unknown=unknown)),
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
            "namespace": record.namespace,
            "family": record.family,
            "module_id": record.module_id,
            "tags": list(record.tags),
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
    )


def _compile_record(
    descriptor: CapabilityDescriptor,
    binding: BoundCapabilityAction,
) -> CompiledToolRecord:
    metadata = dict(descriptor.metadata or {})
    declared_aliases = tuple(str(value or "").strip() for value in descriptor.aliases)
    if len(declared_aliases) != 1 or not declared_aliases[0]:
        raise ValueError(
            f"tool {descriptor.canonical_path or descriptor.name!r} must declare exactly one non-empty alias"
        )
    alias = declared_aliases[0]
    if len(alias) > 64 or re.fullmatch(r"[A-Za-z0-9_-]+", alias) is None:
        raise ValueError(f"invalid tool alias {alias!r}; expected 1-64 characters from [A-Za-z0-9_-]")
    if alias.startswith(("op_", "intro_")):
        raise ValueError(f"tool alias {alias!r} uses the reserved canonical-path namespace")
    canonical_path = str(descriptor.canonical_path or descriptor.name).strip()
    is_mcp = (
        isinstance(metadata.get("mcp"), dict)
        and metadata["mcp"].get("kind") in {"tool", "prompt_render"}
    )

    if is_mcp:
        input_model = None
        output_model = None
        input_schema = dict(descriptor.mcp_input_schema or {"type": "object", "properties": {}})
        output_schema = dict(descriptor.mcp_output_schema or model_validation_schema(McpToolOutput))
        Draft202012Validator.check_schema(input_schema)
        Draft202012Validator.check_schema(output_schema)
        examples = ()
        if descriptor.guidance is None or descriptor.execution is None:
            raise TypeError(f"MCP capability {alias!r} requires guidance and execution semantics")
        guidance = descriptor.guidance
        execution = descriptor.execution
        search_text = str(descriptor.search_text or "").strip()
    else:
        input_model = descriptor.InputModel
        output_model = descriptor.OutputModel
        if input_model is None or output_model is None:
            raise TypeError(f"internal capability {alias!r} requires InputModel and OutputModel")
        input_schema = model_validation_schema(input_model)
        output_schema = model_validation_schema(output_model)
        configured_examples = descriptor.examples
        examples = tuple(dict(item) for item in configured_examples if isinstance(item, dict))
        if input_schema.get("properties") and not examples:
            raise ValueError(
                f"non-empty InputModel for {alias!r} requires at least one example"
            )
        if descriptor.guidance is None or descriptor.execution is None:
            raise TypeError(f"internal capability {alias!r} requires guidance and execution semantics")
        guidance = descriptor.guidance
        execution = descriptor.execution
        search_text = str(descriptor.search_text or "").strip()

    if not search_text:
        raise ValueError(f"capability {alias!r} requires non-empty search_text")

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
        fallback_description=descriptor.description,
    )
    return CompiledToolRecord(
        alias=alias,
        canonical_path=canonical_path,
        target_id=str(descriptor.target_id or SINGLETON_TARGET),
        descriptor_name=descriptor.name,
        module_id=descriptor.module_id,
        family=descriptor.family,
        namespace=str(metadata.get("namespace") or ""),
        tags=tuple(str(item).strip() for item in metadata.get("tags", ()) if str(item).strip()),
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
        is_mcp=is_mcp,
        requires_effect_receipt=execution.effect_kind is not EffectKind.NONE,
    )


def _assert_compatible_target_records(
    existing: CompiledToolRecord,
    candidate: CompiledToolRecord,
) -> None:
    """One public alias may fan out only across equivalent target bindings."""

    target_argument = str(existing.binding.descriptor.metadata.get("target_argument") or "")
    candidate_argument = str(candidate.binding.descriptor.metadata.get("target_argument") or "")
    if not target_argument or target_argument != candidate_argument:
        raise ValueError(
            f"tool alias conflict in generation: {existing.alias} -> incompatible target routing"
        )
    if existing.execution != candidate.execution:
        raise ValueError(
            f"tool alias conflict in generation: {existing.alias} -> incompatible execution semantics"
        )
    if _schema_without_titles(existing.input_schema) != _schema_without_titles(candidate.input_schema):
        raise ValueError(
            f"tool alias conflict in generation: {existing.alias} -> incompatible input schemas"
        )
    if _schema_without_titles(existing.output_schema) != _schema_without_titles(candidate.output_schema):
        raise ValueError(
            f"tool alias conflict in generation: {existing.alias} -> incompatible output schemas"
        )


def _assert_compatible_target_descriptors(
    existing: CapabilityDescriptor,
    candidate: CapabilityDescriptor,
) -> None:
    if existing.execution != candidate.execution or existing.target_kind != candidate.target_kind:
        raise ValueError(
            f"tool alias conflict in generation: {existing.aliases[0]} -> incompatible target semantics"
        )
    if existing.InputModel is None or candidate.InputModel is None:
        raise ValueError(
            f"tool alias conflict in generation: {existing.aliases[0]} -> missing target input model"
        )
    if _schema_without_titles(model_validation_schema(existing.InputModel)) != _schema_without_titles(
        model_validation_schema(candidate.InputModel)
    ):
        raise ValueError(
            f"tool alias conflict in generation: {existing.aliases[0]} -> incompatible input schemas"
        )
    if existing.OutputModel is None or candidate.OutputModel is None:
        raise ValueError(
            f"tool alias conflict in generation: {existing.aliases[0]} -> missing target output model"
        )
    if _schema_without_titles(model_validation_schema(existing.OutputModel)) != _schema_without_titles(
        model_validation_schema(candidate.OutputModel)
    ):
        raise ValueError(
            f"tool alias conflict in generation: {existing.aliases[0]} -> incompatible output schemas"
        )


def _schema_without_titles(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            key: _schema_without_titles(item)
            for key, item in value.items()
            if key != "title"
        }
    if isinstance(value, list | tuple):
        return [_schema_without_titles(item) for item in value]
    return value


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


def _freeze_mapping(value: dict[Any, Any]) -> MappingProxyType:
    return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})


def _freeze_sequence_mapping(value: dict[str, list[str]]) -> MappingProxyType:
    return MappingProxyType({key: tuple(items) for key, items in value.items()})


def _freeze_descriptor(descriptor: CapabilityDescriptor) -> CapabilityDescriptor:
    return replace(
        descriptor,
        aliases=tuple(descriptor.aliases),
        examples=tuple(_deep_freeze(dict(item)) for item in descriptor.examples),
        mcp_input_schema=_deep_freeze(descriptor.mcp_input_schema),
        mcp_output_schema=_deep_freeze(descriptor.mcp_output_schema),
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


def _project_llm_text(
    value: str,
    translations: list[tuple[str, str]],
    *,
    unknown: str = "raise",
) -> str:
    rendered = str(value or "")
    for alias, canonical in translations:
        if canonical not in rendered:
            continue
        rendered = re.sub(
            rf"(?<![A-Za-z0-9_]){re.escape(canonical)}(?![A-Za-z0-9_])",
            alias,
            rendered,
        )
    unresolved = re.search(r"\b(?:op|intro)_[A-Za-z0-9_]+\b", rendered)
    if unresolved is not None:
        if unknown == "preserve":
            return rendered
        if unknown == "redact":
            return re.sub(
                r"\b(?:op|intro)_[A-Za-z0-9_]+\b",
                "[unavailable tool reference]",
                rendered,
            )
        raise ValueError(f"LLM-facing tool text contains unknown canonical path: {unresolved.group(0)}")
    return rendered


def _unambiguous_alias_translations(
    alias_to_canonical: Mapping[str, str],
    *,
    preferred: tuple[str, str] | None = None,
) -> list[tuple[str, str]]:
    aliases_by_canonical: dict[str, list[str]] = {}
    for alias, canonical in alias_to_canonical.items():
        aliases_by_canonical.setdefault(str(canonical), []).append(str(alias))
    translations = [
        (aliases[0], canonical)
        for canonical, aliases in aliases_by_canonical.items()
        if len(aliases) == 1
    ]
    if preferred is not None:
        translations = [item for item in translations if item[1] != preferred[1]]
        translations.append(preferred)
    return sorted(translations, key=lambda item: len(item[1]), reverse=True)


def _project_llm_value(
    value: Any,
    translations: list[tuple[str, str]],
    *,
    unknown: str = "raise",
) -> Any:
    if isinstance(value, str):
        return _project_llm_text(value, translations, unknown=unknown)
    if isinstance(value, dict):
        return {key: _project_llm_value(item, translations, unknown=unknown) for key, item in value.items()}
    if isinstance(value, list):
        return [_project_llm_value(item, translations, unknown=unknown) for item in value]
    return value


__all__ = [
    "CompiledToolRecord",
    "FrozenBoundActionIndex",
    "FrozenCapabilityForest",
    "FrozenCapabilityIndex",
    "FrozenCapabilityRegistry",
    "ToolRegistryGeneration",
    "compile_registry_generation",
]

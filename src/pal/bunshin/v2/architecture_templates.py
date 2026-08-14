from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from importlib import resources
from typing import Any, Mapping, Protocol

from jinja2 import BaseLoader, Environment, StrictUndefined
from jsonschema import Draft202012Validator
import yaml


@dataclass(frozen=True)
class FamilyArchitectureSpecialization:
    specialization_id: str
    family_id: str
    context_schema: Mapping[str, Any]
    module_definition_schema: Mapping[str, Any]
    defs: Mapping[str, Any]
    example: Mapping[str, Any]
    preamble: str
    context_template: str
    module_definition_template: str
    graph_satellite_template: str
    property_data: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class CompiledArchitectureDefinition:
    specialization_id: str
    family_id: str
    schema: Mapping[str, Any]
    template: str
    graph_satellite_template: str
    example: Mapping[str, Any]
    generation_hash: str

    @property
    def property_data(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(
            copy.deepcopy(dict(item))
            for item in list(
                dict(self.schema).get("x-pal-property-data") or []
            )
        )

    @property
    def workspace_authority_rules(self) -> tuple[Mapping[str, Any], ...]:
        rules: list[Mapping[str, Any]] = []
        for property_value in self.property_data:
            rule = dict(property_value.get("workspace_authority") or {})
            if rule:
                rules.append(
                    {
                        "id": str(property_value.get("id") or ""),
                        "property_pointer": str(
                            property_value.get("target_pointer") or ""
                        ),
                        **copy.deepcopy(rule),
                    }
                )
        return tuple(rules)

    def manager_payload(self) -> dict[str, Any]:
        return {
            "specialization_id": self.specialization_id,
            "family_id": self.family_id,
            "schema": copy.deepcopy(dict(self.schema)),
            "template": self.template,
            "graph_satellite_template": self.graph_satellite_template,
            "example": copy.deepcopy(dict(self.example)),
            "generation_hash": self.generation_hash,
        }


class BunshinArchitectureSpecializationProvider(Protocol):
    def declared_bunshin_architecture_specializations(
        self,
    ) -> list[FamilyArchitectureSpecialization | Mapping[str, Any]]:
        ...


@dataclass
class ArchitectureTemplateCompiler:
    providers: tuple[BunshinArchitectureSpecializationProvider, ...] = ()

    def list_specializations(self) -> list[FamilyArchitectureSpecialization]:
        values: dict[str, FamilyArchitectureSpecialization] = {}
        for item in self._builtin_specializations():
            if item.specialization_id in values:
                raise ValueError(
                    "duplicate architecture specialization: "
                    + item.specialization_id
                )
            values[item.specialization_id] = item
        for provider in self.providers:
            declare = getattr(
                provider,
                "declared_bunshin_architecture_specializations",
                None,
            )
            if not callable(declare):
                continue
            for raw in list(declare() or []):
                item = (
                    raw
                    if isinstance(raw, FamilyArchitectureSpecialization)
                    else _specialization_from_mapping(raw)
                )
                _validate_specialization(item)
                if item.specialization_id in values:
                    raise ValueError(
                        "duplicate architecture specialization: "
                        + item.specialization_id
                    )
                values[item.specialization_id] = item
        return [values[key] for key in sorted(values)]

    def compile(self, specialization_id: str) -> CompiledArchitectureDefinition:
        expected = str(specialization_id or "").strip()
        specialization = next(
            (
                item
                for item in self.list_specializations()
                if item.specialization_id == expected
            ),
            None,
        )
        if specialization is None:
            raise ValueError(
                "unknown family architecture specialization: "
                f"{expected or '<empty>'}"
            )
        base_root = resources.files("pal.bunshin").joinpath(
            "architecture_templates",
            "base",
        )
        base_schema = json.loads(
            base_root.joinpath("schema.json").read_text(encoding="utf-8")
        )
        base_template = base_root.joinpath("architect.yaml.j2").read_text(
            encoding="utf-8"
        )
        schema = copy.deepcopy(dict(base_schema))
        schema["$id"] = (
            "pal://bunshin/architectures/"
            + specialization.specialization_id
        )
        schema["x-pal-specialization-id"] = specialization.specialization_id
        if specialization.property_data:
            schema["x-pal-property-data"] = [
                copy.deepcopy(dict(item))
                for item in specialization.property_data
            ]
        definitions = dict(schema.get("$defs") or {})
        definitions["context"] = copy.deepcopy(
            dict(specialization.context_schema)
        )
        definitions["moduleDefinition"] = copy.deepcopy(
            dict(specialization.module_definition_schema)
        )
        for name, value in dict(specialization.defs).items():
            if name in definitions:
                raise ValueError(
                    f"architecture specialization {expected} redefines "
                    f"Manager base definition {name}"
                )
            definitions[str(name)] = copy.deepcopy(value)
        schema["$defs"] = definitions
        schema["examples"] = [
            copy.deepcopy(dict(specialization.example))
        ]
        Draft202012Validator.check_schema(schema)
        errors = sorted(
            Draft202012Validator(schema).iter_errors(
                dict(specialization.example)
            ),
            key=lambda item: list(item.absolute_path),
        )
        if errors:
            raise ValueError(
                f"architecture specialization {expected} example is invalid: "
                + "; ".join(item.message for item in errors[:8])
            )
        for raw_property in specialization.property_data:
            property_value = dict(raw_property)
            context_key = str(
                property_value["target_pointer"]
            ).removeprefix("/context/")
            authoring_example = copy.deepcopy(dict(specialization.example))
            authoring_example.setdefault("context", {})[context_key] = (
                copy.deepcopy(dict(property_value["authoring_shape"]))
            )
            property_errors = sorted(
                Draft202012Validator(schema).iter_errors(authoring_example),
                key=lambda item: list(item.absolute_path),
            )
            if property_errors:
                raise ValueError(
                    f"architecture specialization {expected} property "
                    f"{property_value.get('id')} authoring shape is invalid: "
                    + "; ".join(
                        item.message for item in property_errors[:8]
                    )
                )

        environment = Environment(
            loader=BaseLoader(),
            autoescape=False,
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        template = environment.from_string(base_template).render(
            preamble=specialization.preamble,
            context_template=_context_authoring_template(specialization),
            module_definition_template=(
                specialization.module_definition_template
            ),
        )
        if not template.endswith("\n"):
            template += "\n"
        rendered = yaml.safe_load(template)
        if not isinstance(rendered, Mapping):
            raise ValueError(
                f"architecture specialization {expected} rendered a "
                "non-object template"
            )
        if set(rendered) != {
            "schema_version",
            "graph",
            "context",
            "requirements",
            "modules",
            "scenarios",
        }:
            raise ValueError(
                f"architecture specialization {expected} changed the "
                "Manager-owned authoring envelope"
            )
        generation_hash = _stable_hash(
            {
                "specialization_id": specialization.specialization_id,
                "family_id": specialization.family_id,
                "schema": schema,
                "template": template,
                "graph_satellite_template": (
                    specialization.graph_satellite_template
                ),
            }
        )
        return CompiledArchitectureDefinition(
            specialization_id=specialization.specialization_id,
            family_id=specialization.family_id,
            schema=schema,
            template=template,
            graph_satellite_template=specialization.graph_satellite_template,
            example=copy.deepcopy(dict(specialization.example)),
            generation_hash=generation_hash,
        )

    @staticmethod
    def _builtin_specializations() -> list[FamilyArchitectureSpecialization]:
        root = resources.files("pal.bunshin").joinpath(
            "architecture_specializations"
        )
        if not root.is_dir():
            return []
        result: list[FamilyArchitectureSpecialization] = []
        for directory in sorted(root.iterdir(), key=lambda item: item.name):
            if not directory.is_dir():
                continue
            definition_path = directory.joinpath("specialization.json")
            if not definition_path.is_file():
                continue
            payload = json.loads(
                definition_path.read_text(encoding="utf-8")
            )
            payload.update(
                {
                    "preamble": directory.joinpath("preamble.j2").read_text(
                        encoding="utf-8"
                    ),
                    "context_template": directory.joinpath(
                        "context.j2"
                    ).read_text(encoding="utf-8"),
                    "module_definition_template": directory.joinpath(
                        "module_definition.j2"
                    ).read_text(encoding="utf-8"),
                    "graph_satellite_template": directory.joinpath(
                        "graph_satellite.j2"
                    ).read_text(encoding="utf-8"),
                }
            )
            item = _specialization_from_mapping(payload)
            _validate_specialization(item)
            result.append(item)
        return result


def compiled_architecture_definition_from_mapping(
    value: Mapping[str, Any],
) -> CompiledArchitectureDefinition:
    payload = dict(value or {})
    definition = CompiledArchitectureDefinition(
        specialization_id=str(
            payload.get("specialization_id") or ""
        ).strip(),
        family_id=str(payload.get("family_id") or "").strip(),
        schema=dict(payload.get("schema") or {}),
        template=str(payload.get("template") or ""),
        graph_satellite_template=str(
            payload.get("graph_satellite_template") or ""
        ),
        example=dict(payload.get("example") or {}),
        generation_hash=str(payload.get("generation_hash") or "").strip(),
    )
    if not definition.specialization_id or not definition.family_id:
        raise ValueError(
            "compiled architecture definition requires specialization and family"
        )
    Draft202012Validator.check_schema(dict(definition.schema))
    expected_hash = _stable_hash(
        {
            "specialization_id": definition.specialization_id,
            "family_id": definition.family_id,
            "schema": dict(definition.schema),
            "template": definition.template,
            "graph_satellite_template": definition.graph_satellite_template,
        }
    )
    if not definition.generation_hash or definition.generation_hash != expected_hash:
        raise ValueError("compiled architecture definition hash mismatch")
    return definition


def _specialization_from_mapping(
    value: Mapping[str, Any],
) -> FamilyArchitectureSpecialization:
    payload = dict(value or {})
    return FamilyArchitectureSpecialization(
        specialization_id=str(
            payload.get("specialization_id") or ""
        ).strip(),
        family_id=str(payload.get("family_id") or "").strip(),
        context_schema=dict(payload.get("context_schema") or {}),
        module_definition_schema=dict(
            payload.get("module_definition_schema") or {}
        ),
        defs=dict(payload.get("defs") or {}),
        example=dict(payload.get("example") or {}),
        preamble=str(payload.get("preamble") or ""),
        context_template=str(payload.get("context_template") or ""),
        module_definition_template=str(
            payload.get("module_definition_template") or ""
        ),
        graph_satellite_template=str(
            payload.get("graph_satellite_template") or ""
        ),
        property_data=tuple(
            copy.deepcopy(dict(item))
            for item in list(payload.get("property_data") or [])
        ),
    )


def _validate_specialization(
    value: FamilyArchitectureSpecialization,
) -> None:
    if not value.specialization_id:
        raise ValueError("architecture specialization_id is required")
    if not value.family_id:
        raise ValueError(
            f"architecture specialization {value.specialization_id} "
            "requires family_id"
        )
    if not value.context_schema or not value.module_definition_schema:
        raise ValueError(
            f"architecture specialization {value.specialization_id} "
            "requires context and module definition schemas"
        )
    if not value.example:
        raise ValueError(
            f"architecture specialization {value.specialization_id} "
            "requires an example"
        )
    for name, text in (
        ("preamble", value.preamble),
        ("context template", value.context_template),
        ("module definition template", value.module_definition_template),
        ("graph satellite template", value.graph_satellite_template),
    ):
        if not str(text).strip():
            raise ValueError(
                f"architecture specialization {value.specialization_id} "
                f"requires {name}"
            )
    property_ids: set[str] = set()
    property_targets: set[str] = set()
    expected_fields = {
        "id",
        "target_pointer",
        "guidance",
        "authoring_shape",
        "workspace_authority",
    }
    context_properties = dict(value.context_schema.get("properties") or {})
    required_context = set(value.context_schema.get("required") or [])
    for raw_property in value.property_data:
        property_value = dict(raw_property or {})
        unknown = set(property_value) - expected_fields
        missing = expected_fields - set(property_value)
        if unknown or missing:
            raise ValueError(
                f"architecture specialization {value.specialization_id} "
                "property data has invalid fields"
            )
        property_id = str(property_value.get("id") or "").strip()
        target = str(property_value.get("target_pointer") or "")
        if not property_id or property_id in property_ids:
            raise ValueError(
                f"architecture specialization {value.specialization_id} "
                "property ids must be non-empty and unique"
            )
        property_ids.add(property_id)
        if target in property_targets or not target.startswith("/context/"):
            raise ValueError(
                f"architecture specialization {value.specialization_id} "
                "property targets must be unique direct context JSON pointers"
            )
        property_targets.add(target)
        context_key = target.removeprefix("/context/")
        if "/" in context_key or context_key not in context_properties:
            raise ValueError(
                f"architecture specialization {value.specialization_id} "
                f"property target {target} is not declared by context_schema"
            )
        if context_key not in required_context:
            raise ValueError(
                f"architecture specialization {value.specialization_id} "
                f"property target {target} must be required"
            )
        if not str(property_value.get("guidance") or "").strip():
            raise ValueError(
                f"architecture specialization {value.specialization_id} "
                f"property {property_id} requires authoring guidance"
            )
        if not isinstance(property_value.get("authoring_shape"), Mapping):
            raise ValueError(
                f"architecture specialization {value.specialization_id} "
                f"property {property_id} authoring_shape must be an object"
            )
        rule = dict(property_value.get("workspace_authority") or {})
        expected_rule_fields = {
            "owner_pointer",
            "scopes_pointer",
            "owner_collection_pointer",
            "owner_constraints",
        }
        if set(rule) != expected_rule_fields:
            raise ValueError(
                f"architecture specialization {value.specialization_id} "
                f"property {property_id} workspace_authority has invalid fields"
            )
        for field in (
            "owner_pointer",
            "scopes_pointer",
            "owner_collection_pointer",
        ):
            pointer = str(rule.get(field) or "")
            if not pointer.startswith("/"):
                raise ValueError(
                    f"architecture specialization {value.specialization_id} "
                    f"property {property_id} {field} must be an absolute JSON pointer"
                )
        if not isinstance(rule.get("owner_constraints"), Mapping):
            raise ValueError(
                f"architecture specialization {value.specialization_id} "
                f"property {property_id} owner_constraints must be an object"
            )


def _context_authoring_template(
    specialization: FamilyArchitectureSpecialization,
) -> str:
    parts = [str(specialization.context_template).rstrip()]
    for raw_property in specialization.property_data:
        property_value = dict(raw_property)
        key = str(property_value["target_pointer"]).removeprefix("/context/")
        rendered = yaml.safe_dump(
            {key: copy.deepcopy(dict(property_value["authoring_shape"]))},
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        ).rstrip()
        guidance = " ".join(
            str(property_value["guidance"]).strip().split()
        )
        parts.append(f"# {guidance}\n{rendered}")
    return "\n".join(part for part in parts if part)


def _stable_hash(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

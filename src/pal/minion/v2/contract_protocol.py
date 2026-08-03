from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any, Mapping

import yaml
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ConfigDict, Field
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode

from pal.minion.v2.architecture_templates import (
    CompiledArchitectureDefinition,
)

ARCHITECT_FILENAME = "architect.yaml"
CONTRACT_ARTIFACT = "ContractArtifact"
_SEMANTIC_NAME = re.compile(r"^[a-z][a-z0-9_]*$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ContractRequirement(_StrictModel):
    claim: str = Field(min_length=1)
    owner: str = Field(min_length=1)
    contract_path: list[str] = Field(min_length=1)


class ContractDependency(_StrictModel):
    consumes: list[str] = Field(min_length=1)
    purpose: str = Field(min_length=1)
    handoff: str = Field(min_length=1)


class ContractModule(_StrictModel):
    responsibility: str = Field(min_length=1)
    execution: str
    provides: list[str] = Field(default_factory=list)
    dependencies: dict[str, ContractDependency] = Field(default_factory=dict)
    definition: dict[str, Any]


class ContractEntrypoint(_StrictModel):
    module: str = Field(min_length=1)
    surface: str = Field(min_length=1)


class ContractScenario(_StrictModel):
    modules: list[str] = Field(min_length=1)
    requirement_refs: list[str] = Field(min_length=1)
    entrypoint: ContractEntrypoint
    contract_flow: list[str] = Field(min_length=1)
    observable_behavior: str = Field(min_length=1)
    failure_behavior: str = Field(min_length=1)
    environment: str = Field(min_length=1)


class ContractGraph(_StrictModel):
    sink: str = Field(min_length=1)


class ContractDocument(_StrictModel):
    schema_version: str
    graph: ContractGraph
    context: dict[str, Any]
    requirements: dict[str, ContractRequirement]
    modules: dict[str, ContractModule]
    scenarios: dict[str, ContractScenario]


def validate_contract_payload(
    payload: Mapping[str, Any],
    *,
    definition: CompiledArchitectureDefinition,
) -> ContractDocument:
    value = copy.deepcopy(dict(payload))
    errors = sorted(
        Draft202012Validator(definition.schema).iter_errors(value),
        key=lambda item: list(item.absolute_path),
    )
    if errors:
        rendered: list[str] = []
        for item in errors[:12]:
            path = ".".join(str(part) for part in item.absolute_path)
            rendered.append(f"{path or '$'}: {item.message}")
        raise ValueError(
            "architect.yaml does not match the Manager-compiled family schema: "
            + "; ".join(rendered)
        )
    document = ContractDocument.model_validate(value, strict=True)
    if document.schema_version != "2":
        raise ValueError("architect.yaml schema_version must be 2")
    _validate_contract_graph(
        document,
        specialization_id=definition.specialization_id,
    )
    return document


def load_architect_yaml(
    path: Path,
    *,
    definition: CompiledArchitectureDefinition,
) -> ContractDocument:
    try:
        payload = yaml.load(
            Path(path).read_text(encoding="utf-8"),
            Loader=_ContractYamlLoader,
        )
    except (OSError, yaml.YAMLError, ConstructorError, ComposerError) as exc:
        raise ValueError(f"architect.yaml is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("architect.yaml must contain one mapping")
    _assert_string_mapping_keys(payload)
    return validate_contract_payload(payload, definition=definition)


def read_architect_yaml(path: Path) -> dict[str, Any]:
    """Parse the authored YAML without exposing or applying Manager schema."""

    try:
        payload = yaml.load(
            Path(path).read_text(encoding="utf-8"),
            Loader=_ContractYamlLoader,
        )
    except (OSError, yaml.YAMLError, ConstructorError, ComposerError) as exc:
        raise ValueError(f"architect.yaml is unreadable: {exc}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("architect.yaml must contain one mapping")
    _assert_string_mapping_keys(payload)
    return copy.deepcopy(dict(payload))


def _assert_string_mapping_keys(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    "architect.yaml mapping keys must be strings; "
                    f"{path} contains {key!r}. Quote YAML keyword-like keys "
                    "such as \"on\"."
                )
            _assert_string_mapping_keys(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _assert_string_mapping_keys(child, path=f"{path}[{index}]")


def software_contract_projection(
    contract: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile a software contract into the adapter-neutral skeleton view.

    ``architect.yaml`` remains the only authored truth source. This projection
    exists only to feed the Git/worktree compiler that already understands the
    software module shape.
    """

    modules: dict[str, Any] = {}
    for name, raw in dict(contract.get("modules") or {}).items():
        module = dict(raw or {})
        definition = dict(module.get("definition") or {})
        modules[str(name)] = {
            "module_kind": (
                "implementation"
                if str(module.get("execution") or "") == "produce"
                else "contract_only"
            ),
            "behavior_kind": str(definition.get("behavior_kind") or ""),
            "responsibility": str(module.get("responsibility") or ""),
            "dependencies": {
                str(provider): dict(dependency or {})
                for provider, dependency in dict(
                    module.get("dependencies") or {}
                ).items()
            },
            "contract": dict(definition.get("contract") or {}),
            "ownership": list(definition.get("ownership") or []),
            "lifecycle": dict(definition.get("lifecycle") or {}),
            "state_machine": definition.get("state_machine"),
            "paths": dict(definition.get("paths") or {}),
        }
    return {
        "schema_version": 5,
        "graph": dict(contract.get("graph") or {}),
        "requirements": {
            str(name): dict(value or {})
            for name, value in dict(contract.get("requirements") or {}).items()
        },
        "modules": modules,
        "scenarios": {
            str(name): {
                **dict(value or {}),
                "entrypoint": str(
                    dict(
                        dict(value or {}).get("entrypoint") or {}
                    ).get("surface")
                    or ""
                ),
            }
            for name, value in dict(contract.get("scenarios") or {}).items()
        },
    }


def compile_contract_markdown(
    contract: Mapping[str, Any],
    *,
    requirements_payload: Mapping[str, Any],
) -> str:
    """Render the completed Family-specialized instance for human review.

    The human sees concrete contract values, including the Family-owned
    ``context`` and module ``definition``. Base schemas, specialization patches,
    templates, and compiler metadata never enter this projection.
    """

    context = dict(contract.get("context") or {})
    modules = {
        str(name): dict(value or {})
        for name, value in dict(contract.get("modules") or {}).items()
    }
    requirements = {
        str(name): dict(value or {})
        for name, value in dict(contract.get("requirements") or {}).items()
    }
    scenarios = {
        str(name): dict(value or {})
        for name, value in dict(contract.get("scenarios") or {}).items()
    }
    lines = [
        "# Contract Review",
        "",
        f"- Modules: {len(modules)}",
        f"- Requirements: {len(requirements)}",
        f"- Scenarios: {len(scenarios)}",
        f"- Graph sink: `{dict(contract.get('graph') or {}).get('sink') or ''}`",
        "",
        "## Family context",
        "",
    ]
    _append_yaml_projection(lines, context)
    lines.extend(["", "## Modules", ""])
    for name, module in modules.items():
        dependencies = {
            str(provider): dict(value or {})
            for provider, value in dict(
                module.get("dependencies") or {}
            ).items()
        }
        lines.extend(
            [
                f"### `{name}`",
                "",
                str(module.get("responsibility") or ""),
                "",
                f"- Execution: `{module.get('execution') or ''}`",
                "- Provides: "
                + ", ".join(
                    f"`{item}`"
                    for item in list(module.get("provides") or [])
                ),
                "- Depends on: "
                + (
                    ", ".join(f"`{item}`" for item in dependencies)
                    or "(none)"
                ),
                "",
                "#### Family definition",
                "",
            ]
        )
        _append_yaml_projection(
            lines,
            dict(module.get("definition") or {}),
        )
        if dependencies:
            lines.extend(["", "#### Dependency handoffs", ""])
            _append_yaml_projection(lines, dependencies)
        lines.append("")
    lines.extend(["## Requirement mapping", ""])
    for name, requirement in requirements.items():
        contract_path = " → ".join(
            str(item)
            for item in list(requirement.get("contract_path") or [])
        )
        lines.extend(
            [
                f"### `{name}`",
                "",
                str(requirement.get("claim") or ""),
                "",
                f"- Owner: `{requirement.get('owner') or ''}`",
                f"- Contract path: `{contract_path}`",
                "",
            ]
        )
    lines.extend(["## Scenarios", ""])
    for name, scenario in scenarios.items():
        entrypoint = dict(scenario.get("entrypoint") or {})
        lines.extend(
            [
                f"### `{name}`",
                "",
                "- Modules: "
                + " → ".join(
                    f"`{item}`"
                    for item in list(scenario.get("modules") or [])
                ),
                "- Requirements: "
                + ", ".join(
                    f"`{item}`"
                    for item in list(
                        scenario.get("requirement_refs") or []
                    )
                ),
                "- Entrypoint: "
                f"`{entrypoint.get('module') or ''}` / "
                f"`{entrypoint.get('surface') or ''}`",
                "- Contract flow: "
                + " → ".join(
                    str(item)
                    for item in list(
                        scenario.get("contract_flow") or []
                    )
                ),
                "- Observable behavior: "
                + str(scenario.get("observable_behavior") or ""),
                "- Failure behavior: "
                + str(scenario.get("failure_behavior") or ""),
                "- Environment: "
                + str(scenario.get("environment") or ""),
                "",
            ]
        )
    task_name = str(
        requirements_payload.get("task_name")
        or requirements_payload.get("title")
        or ""
    ).strip()
    if task_name:
        lines[1:1] = [f"Task: {task_name}", ""]
    return "\n".join(lines).rstrip() + "\n"


def _append_yaml_projection(
    lines: list[str],
    value: Mapping[str, Any],
) -> None:
    rendered = yaml.safe_dump(
        dict(value),
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).rstrip()
    if not rendered:
        rendered = "{}"
    lines.extend("    " + line for line in rendered.splitlines())


def _validate_contract_graph(
    document: ContractDocument,
    *,
    specialization_id: str,
) -> None:
    if not document.requirements:
        raise ValueError("contract requires at least one requirement")
    if not document.modules:
        raise ValueError("contract requires at least one module")
    if not document.scenarios:
        raise ValueError("contract requires at least one scenario")
    module_names = set(document.modules)
    requirement_names = set(document.requirements)
    if document.graph.sink not in module_names:
        raise ValueError(
            f"graph sink is unknown: {document.graph.sink}"
        )
    if document.modules[document.graph.sink].execution != "produce":
        raise ValueError("graph sink must be a produced module")
    for collection_name, names in (
        ("requirement", requirement_names),
        ("module", module_names),
        ("scenario", set(document.scenarios)),
    ):
        invalid = sorted(name for name in names if not _SEMANTIC_NAME.fullmatch(name))
        if invalid:
            raise ValueError(
                f"{collection_name} names must be snake_case: "
                + ", ".join(invalid)
            )
    for name, requirement in document.requirements.items():
        if requirement.owner not in module_names:
            raise ValueError(
                f"requirement {name} owner is unknown: {requirement.owner}"
            )
    dependencies: dict[str, set[str]] = {}
    for name, module in document.modules.items():
        if module.execution not in {"produce", "contract_only"}:
            raise ValueError(
                f"module {name} execution must be produce or contract_only"
            )
        if len(set(module.provides)) != len(module.provides):
            raise ValueError(f"module {name} provides duplicate outputs")
        dependencies[name] = set(module.dependencies)
        for provider, dependency in module.dependencies.items():
            if provider not in module_names:
                raise ValueError(
                    f"module {name} depends on unknown module {provider}"
                )
            if provider == name:
                raise ValueError(f"module {name} cannot depend on itself")
            unknown_outputs = set(dependency.consumes) - set(
                document.modules[provider].provides
            )
            if unknown_outputs:
                raise ValueError(
                    f"module {name} consumes undeclared outputs from {provider}: "
                    + ", ".join(sorted(unknown_outputs))
                )
        if specialization_id == "software_engineering.v1":
            _validate_software_module(name, module)
    _assert_acyclic(dependencies)
    covered_requirements: set[str] = set()
    for name, scenario in document.scenarios.items():
        unknown_modules = set(scenario.modules) - module_names
        if unknown_modules:
            raise ValueError(
                f"scenario {name} references unknown modules: "
                + ", ".join(sorted(unknown_modules))
            )
        unknown_requirements = (
            set(scenario.requirement_refs) - requirement_names
        )
        if unknown_requirements:
            raise ValueError(
                f"scenario {name} references unknown requirements: "
                + ", ".join(sorted(unknown_requirements))
            )
        covered_requirements.update(scenario.requirement_refs)
        if scenario.entrypoint.module not in scenario.modules:
            raise ValueError(
                f"scenario {name} entrypoint module must be in scenario.modules"
            )
        selected = set(scenario.modules)
        for module_name in selected:
            missing = {
                provider
                for provider in dependencies[module_name]
                if (
                    document.modules[provider].execution == "produce"
                    and provider not in selected
                )
            }
            if missing:
                raise ValueError(
                    f"scenario {name} omits produced dependencies of "
                    f"{module_name}: " + ", ".join(sorted(missing))
                )
    uncovered = requirement_names - covered_requirements
    if uncovered:
        raise ValueError(
            "contract requirements are not covered by any scenario: "
            + ", ".join(sorted(uncovered))
        )


def _validate_software_module(
    name: str,
    module: ContractModule,
) -> None:
    definition = dict(module.definition)
    contract = dict(definition.get("contract") or {})
    outputs = set(dict(contract.get("outputs") or {}))
    if outputs != set(module.provides):
        raise ValueError(
            f"module {name} provides must exactly match definition.contract.outputs"
        )
    behavior_kind = str(definition.get("behavior_kind") or "")
    state_machine = definition.get("state_machine")
    if behavior_kind == "stateless" and state_machine is not None:
        raise ValueError(
            f"stateless module {name} must use state_machine: null"
        )
    if isinstance(state_machine, Mapping):
        initial = str(state_machine.get("initial") or "")
        states = {
            str(key): dict(value or {})
            for key, value in dict(state_machine.get("states") or {}).items()
        }
        if initial not in states:
            raise ValueError(
                f"module {name} state_machine initial state is undeclared"
            )
        reachable = {initial}
        frontier = [initial]
        while frontier:
            state_name = frontier.pop()
            transitions = dict(states[state_name].get("transitions") or {})
            for transition in transitions.values():
                target = str(dict(transition or {}).get("to") or "")
                if target not in states:
                    raise ValueError(
                        f"module {name} state transition targets undeclared state {target}"
                    )
                if target not in reachable:
                    reachable.add(target)
                    frontier.append(target)
        unreachable = set(states) - reachable
        if unreachable:
            raise ValueError(
                f"module {name} has unreachable states: "
                + ", ".join(sorted(unreachable))
            )
    paths = dict(definition.get("paths") or {})
    if module.execution == "contract_only":
        if str(paths.get("contract_mode") or "") != "file_frozen":
            raise ValueError(
                f"contract_only module {name} must use file_frozen"
            )
        if list(paths.get("implementation_scopes") or []):
            raise ValueError(
                f"contract_only module {name} cannot declare implementation scopes"
            )


def _assert_acyclic(graph: Mapping[str, set[str]]) -> None:
    temporary: set[str] = set()
    permanent: set[str] = set()

    def visit(node: str) -> None:
        if node in permanent:
            return
        if node in temporary:
            raise ValueError(f"contract module dependency cycle includes {node}")
        temporary.add(node)
        for dependency in graph[node]:
            visit(dependency)
        temporary.remove(node)
        permanent.add(node)

    for node in sorted(graph):
        visit(node)


class _ContractYamlLoader(yaml.SafeLoader):
    yaml_implicit_resolvers = copy.deepcopy(
        yaml.SafeLoader.yaml_implicit_resolvers
    )

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            raise ConstructorError(
                None,
                None,
                "YAML aliases are not allowed",
                self.peek_event().start_mark,
            )
        return super().compose_node(parent, index)

    def flatten_mapping(self, node: MappingNode) -> None:
        for key_node, _value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    "YAML merge keys are not allowed",
                    key_node.start_mark,
                )
        super().flatten_mapping(node)

    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        self.flatten_mapping(node)
        result: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in result:
                raise ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"duplicate key is not allowed: {key!r}",
                    key_node.start_mark,
                )
            result[key] = self.construct_object(value_node, deep=deep)
        return result

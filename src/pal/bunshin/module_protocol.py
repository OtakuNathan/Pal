from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


SemanticName = Annotated[
    str,
    StringConstraints(
        min_length=2,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]{1,79}$",
    ),
]
SemanticText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ModuleContractPort(_StrictModel):
    interface: SemanticText
    semantics: SemanticText


class ModuleContract(_StrictModel):
    inputs: dict[SemanticName, ModuleContractPort]
    outputs: dict[SemanticName, ModuleContractPort] = Field(min_length=1)
    errors: list[SemanticText]
    invariants: list[SemanticText]


class ModuleDependency(_StrictModel):
    consumes: list[SemanticName] = Field(min_length=1)
    purpose: SemanticText
    handoff: SemanticText


class ModuleLifecycle(_StrictModel):
    creation: SemanticText
    operation: SemanticText
    shutdown: SemanticText
    failure: SemanticText
    cleanup: SemanticText


class ModuleStateTransition(_StrictModel):
    to: SemanticName
    effect: SemanticText


class ModuleState(_StrictModel):
    meaning: SemanticText
    transitions: dict[SemanticName, ModuleStateTransition]


class ModuleStateMachine(_StrictModel):
    initial: SemanticName
    states: dict[SemanticName, ModuleState] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_graph(self) -> ModuleStateMachine:
        state_names = set(self.states)
        if self.initial not in state_names:
            raise ValueError(f"initial state is not declared: {self.initial}")
        unknown_targets = sorted(
            {
                transition.to
                for state in self.states.values()
                for transition in state.transitions.values()
                if transition.to not in state_names
            }
        )
        if unknown_targets:
            raise ValueError(
                "state transitions reference unknown targets: "
                + ", ".join(unknown_targets)
            )
        reachable = {self.initial}
        pending = [self.initial]
        while pending:
            current = pending.pop()
            for transition in self.states[current].transitions.values():
                if transition.to in reachable:
                    continue
                reachable.add(transition.to)
                pending.append(transition.to)
        unreachable = sorted(state_names - reachable)
        if unreachable:
            raise ValueError(
                "state machine contains unreachable states: "
                + ", ".join(unreachable)
            )
        return self


class ModuleDefinition(_StrictModel):
    module_kind: Literal["implementation", "contract_only"]
    behavior_kind: Literal[
        "stateless",
        "resource_owner",
        "service",
        "workflow",
        "adapter",
    ]
    responsibility: SemanticText
    dependencies: dict[SemanticName, ModuleDependency]
    contract: ModuleContract
    ownership: list[SemanticText] = Field(min_length=1)
    lifecycle: ModuleLifecycle
    state_machine: ModuleStateMachine | None = None

    @model_validator(mode="after")
    def _validate_stateless_shape(self) -> ModuleDefinition:
        if self.behavior_kind == "stateless" and self.state_machine is not None:
            raise ValueError("stateless modules must not declare a state_machine")
        return self

from __future__ import annotations

from typing import Any


class ToolSurface:
    """Project registry generations into LLM tool contracts.

    DIRECT descriptors and explicit owner ``native_function`` opt-ins expose
    full schemas. Opt-ins retain their declared invocation/effect semantics;
    model execution routes them through the same validated runtime path.
    """

    def __init__(self, context) -> None:
        self.context = context

    def build_llm_tool_contracts(self) -> list[dict[str, object]]:
        generation = self.context.execution_runtime.registry_generation
        return [dict(generation.provider_specs[alias]) for alias in sorted(generation.provider_specs)]

    def build_tool_contracts_from_descriptors(self, descriptors: list[Any]) -> list[dict[str, object]]:
        contracts: list[dict[str, object]] = []
        generation = self.context.execution_runtime.registry_generation
        for descriptor in sorted(descriptors, key=lambda item: item.name):
            record = next(
                (
                    item
                    for item in generation.direct_aliases.values()
                    if item.alias in descriptor.aliases
                ),
                None,
            )
            if record is None:
                continue
            contract = generation.provider_specs.get(record.alias)
            if contract is not None:
                contracts.append(dict(contract))
        return contracts

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from pal.core.prompt_compiler import PromptCompiler
from pal.core.prompt_fragment_registry import PromptFragmentRegistry
from pal.shared import PromptAssemblyContext, PromptFragment


@dataclass
class _Provider:
    provider_id: str
    module_id: str
    fragments: tuple[PromptFragment, ...] = ()

    def build_prompt_fragments(
        self,
        _context: PromptAssemblyContext,
    ) -> list[PromptFragment]:
        return list(self.fragments)


def _compiler(*providers: _Provider) -> PromptCompiler:
    registry = PromptFragmentRegistry()
    for provider in providers:
        registry.register(provider)
    return PromptCompiler(
        SimpleNamespace(
            prompt_fragment_registry=registry,
            execution_runtime=None,
            port_registry={},
            require_port=lambda _key: (_ for _ in ()).throw(KeyError(_key)),
        )
    )


def test_output_contract_is_projected_into_prompt_ir() -> None:
    compiler = _compiler(
        _Provider(
            provider_id="test.output_contract",
            module_id="test",
            fragments=(
                PromptFragment(
                    section="task_acceptance",
                    title="Bound Invocation",
                    content="Perform the bound task.",
                ),
                PromptFragment(
                    section="output_contract",
                    title="Output Contract",
                    content="Call the terminal submission tool exactly once.",
                ),
            ),
        )
    )

    prompt_ir = compiler.build_prompt_ir(
        PromptAssemblyContext(metadata={"memory_pack": None})
    )

    assert [block.block_id for block in prompt_ir.system_blocks] == [
        "task_acceptance",
        "output_contract",
    ]
    assert prompt_ir.system_blocks[-1].content == (
        "Call the terminal submission tool exactly once."
    )


def test_unknown_prompt_section_fails_closed() -> None:
    compiler = _compiler(
        _Provider(
            provider_id="test.typo",
            module_id="test",
            fragments=(
                PromptFragment(
                    section="ouptut_contract",
                    title="Typo",
                    content="This must not disappear silently.",
                ),
            ),
        )
    )

    with pytest.raises(ValueError, match="unknown prompt fragment section"):
        compiler.build_prompt_ir(
            PromptAssemblyContext(metadata={"memory_pack": None})
        )


def test_prompt_provider_id_collision_is_rejected_without_changing_owner() -> None:
    registry = PromptFragmentRegistry()
    original = _Provider(provider_id="shared.id", module_id="module_a")
    collision = _Provider(provider_id="shared.id", module_id="module_b")
    registry.register(original)

    with pytest.raises(ValueError, match="prompt fragment provider already registered"):
        registry.register(collision)

    assert registry.list_for_prompt() == (original,)
    assert registry.by_module == {"module_a": ["shared.id"]}


def test_registering_same_prompt_provider_object_is_idempotent() -> None:
    registry = PromptFragmentRegistry()
    provider = _Provider(provider_id="same.id", module_id="module_a")

    registry.register(provider)
    registry.register(provider)

    assert registry.list_for_prompt() == (provider,)
    assert registry.by_module == {"module_a": ["same.id"]}

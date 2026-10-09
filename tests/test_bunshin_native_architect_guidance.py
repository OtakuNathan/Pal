"""The native runner submits files before completion; external adapters own submission."""
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tomllib

import pytest

from pal.bunshin.harness_request import compile_architect_harness_request
from pal.bunshin.prompt_adapter import BunshinPromptFragmentProvider
from pal.bunshin.runner_components.prompt_context import PromptContext
from pal.core.prompt_compiler import PromptCompiler
from pal.core.prompt_fragment_registry import PromptFragmentRegistry
from pal.shared import BunshinInvocationPack, PromptAssemblyContext


PROFILE = Path(__file__).resolve().parents[1] / "src/pal/bunshin/profile_templates/software_engineering/v2_architect.toml"


def pack(root, *, harness="pal", role="architect", receipt=True):
    profile = tomllib.loads(PROFILE.read_text())
    binding = {"role": role, "mode": "author", "submission_receipt_required": receipt,
               "authoring_input_fingerprint": "pinned-input"}
    if harness is not None:
        binding["harness_id"] = harness
    return BunshinInvocationPack(
        invocation_id="logical-architect", goal="Author the bound architecture.",
        bunshin_profile="software_engineering.v2_architect", resolved_profile=profile,
        allowed_capabilities=["op_bunshin_contract_submit"],
        workspace={"repo_path": str(root), "architect_path": str(root / "architect.yaml")},
        metadata={"bunshin_v2": binding},
    )


def render(value, root):
    context = PromptContext(SimpleNamespace(visible_capability_aliases=["submit_contract"]), value, root)
    registry = PromptFragmentRegistry()
    registry.register(BunshinPromptFragmentProvider(context.prompt_scaffold, lambda: ""))
    compiler = PromptCompiler(SimpleNamespace(
        prompt_fragment_registry=registry, execution_runtime=None, port_registry={},
        require_port=lambda key: (_ for _ in ()).throw(KeyError(key)),
    ))
    ir = compiler.build_prompt_ir(PromptAssemblyContext(metadata={"memory_pack": None}))
    return "\n".join(block.content for block in ir.developer_blocks)


@pytest.mark.parametrize("restored", [False, True])
@pytest.mark.parametrize("harness", ["pal", None])
def test_consumed_native_architect_requires_submit_receipt_without_mutating_pinned_pack(tmp_path, restored, harness):
    value = pack(tmp_path, harness=harness)
    original = copy.deepcopy(value.to_dict())
    if restored:
        value = BunshinInvocationPack.from_dict(copy.deepcopy(original))
    consumed = render(value, tmp_path)
    context = PromptContext(SimpleNamespace(visible_capability_aliases=["submit_contract"]), value, tmp_path)
    assert context.output_contract().count("submit_contract") == 1
    assert "after the harness finishes" not in consumed
    assert "submit_contract" in consumed and "empty argument object ({})" in consumed
    assert "durable submission receipt" in consumed
    assert "A final response does not submit the files" in consumed
    assert value.to_dict() == original


def test_external_architect_keeps_manager_recording_contract(tmp_path):
    value = pack(tmp_path, harness="codex_architect")
    original = copy.deepcopy(value.to_dict())
    consumed = render(value, tmp_path)
    assert "after the harness finishes" in consumed
    assert "durable submission receipt" not in consumed
    assert value.to_dict() == original


def test_external_adapter_still_compiles_harness_neutral_submission_guidance(tmp_path):
    value = pack(tmp_path, harness="codex_architect")
    # The external compiler requires a harness-neutral behavior fragment.
    # Its existing rejection of Pal-specific behavior is outside this fix.
    value = replace(value, resolved_profile={**value.resolved_profile,
        "behavior_fragment": "Author the bound architecture and declaration files."})
    original = copy.deepcopy(value.to_dict())
    render(value, tmp_path)
    request = compile_architect_harness_request(value)
    assert "after the harness finishes" in request.developer_instructions
    assert "submit_contract" not in request.developer_instructions + request.user_input
    assert value.to_dict() == original


@pytest.mark.parametrize("role,receipt", [("verifier", True), ("architect", False)])
def test_unrelated_role_or_unbound_completion_guidance_is_unchanged(tmp_path, role, receipt):
    value = pack(tmp_path, role=role, receipt=receipt)
    context = PromptContext(SimpleNamespace(visible_capability_aliases=[]), value, tmp_path)
    assert context.prompt_scaffold()["output_contract"] == value.resolved_profile["output_contract_fragment"]

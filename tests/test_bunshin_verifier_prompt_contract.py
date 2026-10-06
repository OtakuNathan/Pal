"""Exercise consumed module/sink prompts and the required checklist spine.

These are offline prompt-contract tests, not claims about model behavior or
TLC coverage. They use the live reference, mode-fragment, playbook, scaffold,
and PromptIR paths rather than inspecting only the generic TOML text.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pal.bunshin.prompt_adapter import (
    BunshinPromptFragmentProvider,
    render_bunshin_task_prompt,
)
from pal.bunshin.runner_components.prompt_context import PromptContext
from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.catalog import BunshinV2Catalog
from pal.bunshin.v2.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.v2.semantic_orchestration.attempt_models import RoleAttemptRequest
from pal.bunshin.v2.semantic_orchestration.attempt_playbook_binding import PlaybookBinding
from pal.bunshin.v2.semantic_orchestration.attempt_prompt_construction import PromptConstruction
from pal.bunshin.v2.semantic_orchestration.attempt_reference_binding import ReferenceBinding
from pal.bunshin.v2.semantic_orchestration.attempt_verifier_context import VerifierContext
from pal.bunshin.v2.semantic_orchestration.workflow_facts import WorkflowFacts
from pal.bunshin.v2.task_ledger import TaskLedgerService
from pal.bunshin.v2.verification_builder import VERIFICATION_BUILDER_TOOL_SPECS
from pal.bunshin.v2.work_items import update_checklist_tool_result
from pal.core.prompt_compiler import PromptCompiler
from pal.core.prompt_fragment_registry import PromptFragmentRegistry
from pal.shared import BunshinInvocationPack, PromptAssemblyContext
from pal.shared.tool_protocol import new_tool_call


async def _bound_pack(root, *, sink, legacy_playbook=False):
    repository = BunshinV2Repository(root)
    artifacts = ContentAddressedArtifactStore(root, repository.artifacts)
    binding_ref = BunshinV2Catalog(root, artifacts).publish_family_binding(
        "software_engineering.v2_coder"
    )
    binding = dict(artifacts.read_json(binding_ref))
    if legacy_playbook:
        # Existing family bindings retain their pinned four-phase playbook.
        steps = binding["role_bindings"]["verifier"]["role_profile"]["role"]["playbook"]["steps"]
        steps.insert(2, {
            "key": "smash_green",
            "instruction": "Use bounded adversarial cases when required checks are green.",
            "done_when": "Plausible breakage is ruled out or recorded.",
        })
    view_ref = artifacts.put_json(
        {"module_name": "cli" if sink else "parser", "graph_sink": sink},
        artifact_type="ModuleWorkViewArtifact",
    )
    refs = {"module_work_view": view_ref}
    if sink:
        refs["system_delivery_view"] = artifacts.put_json(
            {
                "entrypoints": [{"kind": "cli", "command": "python -m app"}],
                "scenarios": {"valid input": {}, "leading TAB rejected": {}},
            },
            artifact_type="SystemDeliveryViewArtifact",
        )
    lease = repository.leases.claim_lease("prompt-contract", "attempt-current", ttl_seconds=60)
    command = RoleAttemptRequest(
        effect={"effect_id": "effect-prompt"},
        snapshot=AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id="node-prompt",
            workflow_id="workflow-prompt", state="verifying", version=1,
            payload={"execution_adapter": "software_git.v2"},
            created_at="", updated_at="",
        ),
        invocation_id="attempt-current", lease_resource=lease.resource_key,
        fencing_token=lease.fencing_token, profile="software_engineering.v2_verifier",
        activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
        instruction="Verify the bound CLI Candidate." if sink else "Verify the bound parser Candidate.",
        reference_refs=refs, workspace_override=None, prepare_workspace=False,
    )
    # Only workspace preparation is outside this test. All subsequent prompt
    # compilation and role/checklist binding stages are the production path.
    prepared = SimpleNamespace(
        binding=binding, binding_ref=binding_ref.to_dict(),
        bound_input_entries=[], bound_reference_refs=refs,
        contract_authoring=False, family_policies=dict(binding.get("policies") or {}),
        llm_policy={}, mode="module", role="verifier",
        workspace={"runtime_root": str(root), "repo_path": str(root / "repo")},
    )
    facts = WorkflowFacts(artifacts, repository)
    verifier = await VerifierContext(artifacts).execute(command, prepared)
    references = await ReferenceBinding(
        repository, TaskLedgerService(root, artifacts), facts,
    ).execute(command, prepared)
    prompt = await PromptConstruction(facts).execute(command, references, verifier, prepared)
    bound = await PlaybookBinding(artifacts).execute(command, prompt, prepared)
    return bound.pack


def _render(pack, root):
    contract = pack.metadata["bunshin_v2"]["verification_tool_contract"]
    aliases = [
        spec["alias"] for capability, spec in VERIFICATION_BUILDER_TOOL_SPECS.items()
        if capability in contract["allowed_capabilities"] and capability in pack.allowed_capabilities
    ]
    context = PromptContext(SimpleNamespace(visible_capability_aliases=aliases), pack, root)
    provider = BunshinPromptFragmentProvider(context.prompt_scaffold, context.render_durable_role_context)
    registry = PromptFragmentRegistry()
    registry.register(provider)
    compiler = PromptCompiler(SimpleNamespace(
        prompt_fragment_registry=registry, execution_runtime=None, port_registry={},
        require_port=lambda key: (_ for _ in ()).throw(KeyError(key)),
    ))
    ir = compiler.build_prompt_ir(PromptAssemblyContext(metadata={"memory_pack": None}))
    developer = "\n".join(block.content for block in ir.developer_blocks)
    return developer, render_bunshin_task_prompt(pack), context


@pytest.mark.parametrize("sink", [False, True], ids=["module", "sink"])
def test_consumed_prompt_preserves_evidence_and_completion_gates(tmp_path, sink):
    pack = asyncio.run(_bound_pack(tmp_path, sink=sink))
    developer, user, context = _render(pack, tmp_path)
    full = developer + "\n" + user

    assert "exactly one module Candidate" in developer
    assert "Manager-bound contract as adjudication truth" not in developer  # Generic fallback was replaced.
    for rule in (
        "accepted contract is authoritative: never weaken it",
        "cannot repair product code",
        "every named current/historical RepairBill reproducer",
        "record every result even when one fails",
        "never permits skipping this bounded current-delta review",
        "genuinely independent verifier-owned boundary checks",
        "risk-matched repository-supported dynamic analyzers",
        "PASS still requires evidence adequate to the identified risk",
        "positive and negative consumer compile probe against the actual public API",
        "missing required enforcement is blocking",
        "execute affected checks after the final corpus edit",
        "complete the required checklist",
        "ready_by_outcome and blockers_by_outcome",
        "for state/evidence readiness",
        "supply its listed outcome_arguments at submission",
        "failed or UNKNOWN check is not PASS",
        "For UNKNOWN, supply the concrete environmental reason and follow-up verification plan",
        "advisory p2 entries do not prevent PASS",
        "exactly one semantic outcome",
    ):
        assert rule in developer, rule
    assert developer.index("First replay") < developer.index("Then inspect the current Git review range")
    assert "current/historical regression" in user
    assert "missing_historical_cases" in user
    assert "read_verification_draft_status" in context.tool_session.visible_capability_aliases
    assert "extra_obligations" not in full
    assert "write_verification_scratch" not in full
    assert "SFINAE" not in developer and "concepts/requires" not in developer
    assert "For bound static contracts" in developer


def test_current_candidate_reuses_analysis_and_cases_without_second_attack_phase(tmp_path):
    pack = asyncio.run(_bound_pack(tmp_path, sink=False))
    developer, user, _ = _render(pack, tmp_path)
    for rule in (
        "independently inspect their assertions",
        "never rely on producer claims for acceptance",
        "only for a demonstrated coverage gap",
        "Reuse unchanged contract analysis and coverage mapping",
        "rerun the required evidence on the current Candidate",
        "prior Candidate's verdict or execution cannot settle this assignment",
        "multiple contract risks and checklist obligations",
        "Still record every required classified policy obligation",
        "Existing verifier cases can supply these attacks",
        "do not invent another test merely to start a separate adversarial phase",
    ):
        assert rule in developer, rule
    assert "do not rerun it solely to obtain an ordinary-shell receipt" in user
    steps = pack.metadata["bunshin_v2"]["role_protocol"]["playbook"]["steps"]
    assert [step["key"] for step in steps] == ["regress", "changed_boundary", "verdict"]
    assert "independently challenge material boundaries" in steps[1]["instruction"]
    assert "required classified policy evidence remains necessary" in steps[1]["done_when"]
    assert "mark this checklist item complete before submission" in steps[2]["done_when"]

    workspace = {**pack.workspace, "bunshin_v2": pack.metadata["bunshin_v2"]}
    reordered = update_checklist_tool_result(new_tool_call(
        name="op_bunshin_update_checklist", call_id="reorder-risk-before-history",
        args={"plan": [
            {"step": step, "status": "pending"}
            for step in ("changed boundary", "regress", "verdict")
        ]},
    ), workspace)
    assert not reordered.ok
    assert "order" in reordered.llm_text.lower()


def test_sink_retains_every_system_scenario_and_real_entrypoint_gate(tmp_path):
    pack = asyncio.run(_bound_pack(tmp_path, sink=True))
    developer, user, _ = _render(pack, tmp_path)
    assert "separate Manager-compiled SystemDeliveryView" in developer
    assert "every required system scenario through the real public surface" in developer
    assert "interactive TTY with a PTY-style harness" in developer
    assert "invoke the real CLI" in developer
    assert "start the real service and use its client" in developer
    assert "reference:system_delivery_view" in user
    seed = pack.metadata["bunshin_v2"]["work_item_seed"]
    assert [item["summary"] for item in seed] == [
        "regress", "changed boundary", "verdict",
        "verify system scenario: leading TAB rejected", "verify system scenario: valid input",
    ]
    assert all(item["required"] for item in seed)
    workspace = {**pack.workspace, "bunshin_v2": pack.metadata["bunshin_v2"]}
    omitted_scenario = update_checklist_tool_result(new_tool_call(
        name="op_bunshin_update_checklist", call_id="omit-sink-scenario",
        args={"plan": [{"step": item["summary"], "status": "completed"} for item in seed[:-1]]},
    ), workspace)
    assert not omitted_scenario.ok
    assert "verify system scenario: valid input" in omitted_scenario.llm_text


@pytest.mark.parametrize("sink", [False, True], ids=["module", "sink"])
def test_exact_verifier_invocation_survives_without_original_user_turn(tmp_path, sink):
    pack = asyncio.run(_bound_pack(tmp_path, sink=sink))
    # Reconstruct the immutable pack, then inspect only developer PromptIR.
    # No original user turn is supplied to the compiler, as can happen after
    # compaction. A pointer to that old turn is not an adequate substitute.
    restored = BunshinInvocationPack.from_dict(pack.to_dict())
    developer, _, _ = _render(restored, tmp_path)
    assert pack.instruction in developer
    for criterion in pack.acceptance_criteria:
        assert criterion in developer


def test_distinct_brief_acceptance_and_non_verifier_invocations_are_preserved(tmp_path):
    pack = asyncio.run(_bound_pack(tmp_path, sink=False))
    value = pack.to_dict()
    value["metadata"]["requirements_brief"]["acceptance_criteria"] = ["Additional bound acceptance."]
    developer, user, _ = _render(BunshinInvocationPack.from_dict(value), tmp_path)
    assert "Additional bound acceptance." in developer
    assert pack.acceptance_criteria[0] in user
    value["metadata"]["requirements_brief"].pop("acceptance_criteria")
    value["metadata"]["bunshin_v2"]["role"] = "implementation"
    value["metadata"]["bunshin_v2"]["mode"] = "produce"
    developer, user, _ = _render(BunshinInvocationPack.from_dict(value), tmp_path)
    assert pack.instruction in developer and pack.instruction in user


def test_old_pinned_playbook_keeps_its_required_phase(tmp_path):
    pack = asyncio.run(_bound_pack(tmp_path, sink=False, legacy_playbook=True))
    developer, user, _ = _render(pack, tmp_path)
    assert "Preserve every phase in the bound playbook" in user
    assert "do not add a separate adversarial phase" not in user
    seed = pack.metadata["bunshin_v2"]["work_item_seed"]
    assert [item["summary"] for item in seed] == [
        "regress", "changed boundary", "smash green", "verdict",
    ]
    workspace = {**pack.workspace, "bunshin_v2": pack.metadata["bunshin_v2"]}
    omitted_phase = update_checklist_tool_result(new_tool_call(
        name="op_bunshin_update_checklist", call_id="omit-pinned-phase",
        args={"plan": [
            {"step": item["summary"], "status": "completed"}
            for item in seed if item["summary"] != "smash green"
        ]},
    ), workspace)
    assert not omitted_phase.ok
    assert "preserve the profile playbook steps as its ordered prefix" in omitted_phase.llm_text

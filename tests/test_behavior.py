from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from pal.behavior import (
    AFFORDANCE_ACTIVATION_DELIBERATIVE,
    AFFORDANCE_AVAILABLE,
    AFFORDANCE_PARTIAL,
    AFFORDANCE_SOURCE_DECLARED,
    AFFORDANCE_SOURCE_INSTRUCTED,
    AFFORDANCE_SOURCE_LEARNED,
    AFFORDANCE_UNAVAILABLE,
    AFFORDANCE_VISIBILITY_RESIDENT,
    AffordanceDescriptor,
    AffordanceSubmitTool,
    BehaviorAdviceRequest,
    BehaviorAdviceTool,
    BehaviorAffordanceModel,
    BehaviorRepository,
    BehaviorService,
    BehaviorSkillModel,
    SkillDescriptor,
    affordance,
    register_with_core as register_behavior_with_core,
    skill,
)
from pal.behavior.prompt import BehaviorPromptFragmentProvider
from pal.channel import ChannelEnvelope, ChannelRuntime, EndpointConfig, ResponseHandle, register_with_core as register_channel_with_core
from pal.core import PalCore, register_with_core as register_core_with_core
from pal.execution import CapabilityDescriptor
from pal.foundation import EventEnvelope, PalV2Database
from pal.llm import CanonicalLLMOutcome, CanonicalToolCall, LLMPreflightAdvice
from pal.memory import L2Entry, MemoryPack, MemoryService, register_with_core as register_memory_with_core
from pal.memory.models import MemoryCaseModel
from pal.memory.prompt import MemoryPromptFragmentProvider
from pal.skill import SkillInjectTool, SkillService
from pal.shared import MountedSubtreeHandle, PromptAssemblyContext


class _FakeExecutionRuntime:
    def __init__(self, available: set[str] | None = None) -> None:
        self.available = set(available or set())

    def get_capability_spec(self, name: str):
        if name in self.available:
            return {"name": name, "canonical_path": name}
        return None


@dataclass
class _FakeHandle:
    module_id: str
    introspection_provider: object


class _ScriptedLLMRuntime:
    def __init__(self, outcomes: list[CanonicalLLMOutcome]) -> None:
        self.outcomes = outcomes
        self.requests = []

    def preflight(self, request) -> LLMPreflightAdvice:
        self.requests.append(("preflight", request))
        return LLMPreflightAdvice(status="ready", active_model="stub-model", reserved_output_tokens=request.max_output_tokens)

    def generate(self, request):
        self.requests.append(("generate", request))
        if self.outcomes:
            return self.outcomes.pop(0)
        return CanonicalLLMOutcome(text="done", tool_calls=[], finish_reason="stop")


class BehaviorSubsystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_behavior_test_"))
        self.database = PalV2Database(self.root / "pal_behavior.sqlite3")
        self.database.initialize([BehaviorAffordanceModel, BehaviorSkillModel, MemoryCaseModel])
        self.repository = BehaviorRepository()
        self.runtime = _FakeExecutionRuntime(available={"cap.known"})
        self.service = BehaviorService(repository=self.repository, execution_runtime=self.runtime)
        self.skill_service = SkillService(repository=self.repository.skill_repository, behavior_repository=self.repository)

    def tearDown(self) -> None:
        self.database.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_disabled_affordance_and_disabled_skill_are_not_advised(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="disabled.affordance",
                module_id="test",
                title="Commit code",
                scenario_text="commit code",
                prompt_hint="Use commit flow",
                activation_terms=("commit", "code"),
                enabled=False,
                activation_threshold=0.0,
            )
        )
        self.repository.upsert_skill(
            SkillDescriptor(
                skill_id="disabled.skill",
                module_id="test",
                title="Disabled skill",
                summary="disabled",
                manual_text="Do not use.",
                enabled=False,
            )
        )
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="requires.disabled.skill",
                module_id="test",
                title="Commit code with disabled skill",
                scenario_text="commit code",
                prompt_hint="Inject disabled skill",
                skill_refs=("disabled.skill",),
                activation_terms=("commit", "code"),
                activation_threshold=0.0,
            )
        )

        result = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="commit code")))

        self.assertEqual(result.candidates, ())

    def test_behavior_advise_does_not_return_recursive_route(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="recursive",
                module_id="test",
                title="Ask behavior advice",
                scenario_text="need advice",
                prompt_hint="Call behavior advice again",
                capability_refs=("op_behavior_advise",),
                activation_terms=("advice",),
                activation_threshold=0.0,
            )
        )

        result = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="need advice")))

        self.assertEqual(result.candidates, ())

    def test_unavailable_refs_return_candidates_with_availability(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="partial.route",
                module_id="test",
                title="Partial route",
                scenario_text="open the door",
                prompt_hint="Use available and missing cap",
                capability_refs=("cap.known", "cap.missing"),
                activation_terms=("open", "door"),
                activation_threshold=0.0,
            )
        )
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="unavailable.route",
                module_id="test",
                title="Unavailable route",
                scenario_text="ring the bell",
                prompt_hint="Use missing cap",
                capability_refs=("cap.absent",),
                activation_terms=("ring", "bell"),
                activation_threshold=0.0,
            )
        )

        result = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="open the door and ring the bell", top_k=10)))
        availability = {candidate.affordance_id: candidate.availability for candidate in result.candidates}

        self.assertEqual(availability["partial.route"], AFFORDANCE_PARTIAL)
        self.assertEqual(availability["unavailable.route"], AFFORDANCE_UNAVAILABLE)

    def test_resident_affordance_budget_sorting_is_deterministic(self) -> None:
        service = BehaviorService(repository=self.repository, execution_runtime=self.runtime, resident_prompt_budget=3)
        for item in (
            AffordanceDescriptor(
                affordance_id="learned-high",
                module_id="test",
                title="Learned",
                scenario_text="status",
                prompt_hint="Maybe update status.",
                visibility_mode=AFFORDANCE_VISIBILITY_RESIDENT,
                activation_kind=AFFORDANCE_ACTIVATION_DELIBERATIVE,
                source_kind=AFFORDANCE_SOURCE_LEARNED,
                priority=1000,
                updated_at="2026-04-24T00:00:00+00:00",
            ),
            AffordanceDescriptor(
                affordance_id="declared-low",
                module_id="test",
                title="Declared",
                scenario_text="status",
                prompt_hint="Update declared status.",
                visibility_mode=AFFORDANCE_VISIBILITY_RESIDENT,
                activation_kind=AFFORDANCE_ACTIVATION_DELIBERATIVE,
                source_kind=AFFORDANCE_SOURCE_DECLARED,
                priority=10,
                updated_at="2026-04-24T00:00:00+00:00",
            ),
            AffordanceDescriptor(
                affordance_id="instructed-a",
                module_id="test",
                title="Instructed A",
                scenario_text="status",
                prompt_hint="Update instructed A.",
                visibility_mode=AFFORDANCE_VISIBILITY_RESIDENT,
                activation_kind=AFFORDANCE_ACTIVATION_DELIBERATIVE,
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                priority=5,
                updated_at="2026-04-24T00:00:00+00:00",
            ),
            AffordanceDescriptor(
                affordance_id="instructed-b",
                module_id="test",
                title="Instructed B",
                scenario_text="status",
                prompt_hint="Update instructed B.",
                visibility_mode=AFFORDANCE_VISIBILITY_RESIDENT,
                activation_kind=AFFORDANCE_ACTIVATION_DELIBERATIVE,
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                priority=5,
                updated_at="2026-04-24T00:00:00+00:00",
            ),
        ):
            self.repository.upsert_affordance(item)

        ordered = service.resident_affordances()

        self.assertEqual([item.affordance_id for item in ordered], ["instructed-a", "instructed-b", "declared-low"])

    def test_skill_inject_returns_structured_failure_for_missing_or_disabled(self) -> None:
        tool = SkillInjectTool(service=self.skill_service)
        self.repository.upsert_skill(
            SkillDescriptor(
                skill_id="disabled",
                module_id="test",
                title="Disabled",
                summary="disabled",
                manual_text="Disabled manual.",
                enabled=False,
            )
        )

        missing = tool.invoke({"skill_id": "missing"})
        disabled = tool.invoke({"skill_id": "disabled"})

        self.assertEqual(missing.structured["reason"], "skill_not_found_or_inactive")
        self.assertEqual(disabled.structured["reason"], "skill_not_found_or_inactive")

    def test_declared_detach_removes_search_hit_but_instructed_and_learned_remain_unavailable(self) -> None:
        @skill(
            skill_id="declared.skill",
            title="Declared Skill",
            summary="Use declared skill.",
            manual_text="Step 1: use the declared capability.",
        )
        @affordance(
            affordance_id="declared.affordance",
            title="Declared affordance",
            scenario_text="declared scenario",
            prompt_hint="Use declared behavior.",
            activation_terms=("declared", "scenario"),
            capability_refs=("cap.declared",),
            activation_threshold=0.0,
        )
        class DeclaredProvider:
            module_id = "declared_plugin"

        handle = _FakeHandle(module_id="declared_plugin", introspection_provider=DeclaredProvider())
        self.service.register_declared_module(handle)

        before = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="declared scenario")))
        self.assertEqual(before.candidates[0].affordance_id, "declared.affordance")

        self.service.unregister_declared_module("declared_plugin")
        after = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="declared scenario")))
        self.assertEqual(after.candidates, ())

        for source_kind, affordance_id in (
            (AFFORDANCE_SOURCE_INSTRUCTED, "instructed.missing"),
            (AFFORDANCE_SOURCE_LEARNED, "learned.missing"),
        ):
            self.repository.upsert_affordance(
                AffordanceDescriptor(
                    affordance_id=affordance_id,
                    module_id="user",
                    title=affordance_id,
                    scenario_text="missing capability",
                    prompt_hint="Use missing capability.",
                    source_kind=source_kind,
                    capability_refs=("cap.missing",),
                    activation_terms=("missing", "capability"),
                    activation_threshold=0.0,
                )
            )

        retained = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="missing capability", top_k=10)))
        self.assertEqual({candidate.availability for candidate in retained.candidates}, {AFFORDANCE_UNAVAILABLE})

    def test_declared_resident_affordance_registers_prompt_provider_without_database_record(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_behavior_with_core(core.context, self.service)

        @affordance(
            affordance_id="declared.resident",
            title="Declared resident",
            scenario_text="declared resident scenario",
            prompt_hint="Consider declared resident guidance.",
            visibility_mode=AFFORDANCE_VISIBILITY_RESIDENT,
        )
        class DeclaredResidentProvider:
            module_id = "declared_resident_plugin"

        handle = _FakeHandle(module_id="declared_resident_plugin", introspection_provider=DeclaredResidentProvider())
        self.service.register_declared_module(handle)

        self.assertIsNone(self.repository.get_affordance("declared.resident"))
        prompt = core.build_canonical_prompt(PromptAssemblyContext())
        system = prompt.messages[0]["content"]

        self.assertIn("## Resident Affordances", system)
        self.assertIn("Declared resident", system)
        self.assertIn("Consider declared resident guidance.", system)
        self.assertIn("resident_affordances", prompt.metadata["fragment_sections"])

        self.service.unregister_declared_module("declared_resident_plugin")
        after = core.build_canonical_prompt(PromptAssemblyContext()).messages[0]["content"]
        self.assertNotIn("Declared resident", after)

    def test_learned_affordance_uses_weak_wording(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="learned.strong",
                module_id="test",
                title="Learned strong",
                scenario_text="oled mood",
                prompt_hint="You must always update OLED mood.",
                source_kind=AFFORDANCE_SOURCE_LEARNED,
                activation_terms=("oled", "mood"),
                activation_threshold=0.0,
            )
        )

        result = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="oled mood changed")))
        hint = result.candidates[0].prompt_hint.lower()

        self.assertIn("consider", hint)
        self.assertNotIn("must", hint)
        self.assertNotIn("always", hint)
        self.assertNotIn("should", hint)

    def test_memory_query_hints_are_returned_without_recall_and_memory_cases_are_not_affordances(self) -> None:
        MemoryCaseModel.create(
            case_id="case-1",
            title="Commit case",
            summary="A memory case, not an affordance.",
            situation_text="commit code",
            task_text="commit code",
            action_text="remember only",
            result_text="done",
            search_text="commit code",
        )
        without_affordance = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="commit code")))
        self.assertEqual(without_affordance.candidates, ())

        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="memory.hint",
                module_id="test",
                title="Memory hint",
                scenario_text="commit code",
                prompt_hint="Consider recalling commit preferences.",
                activation_terms=("commit", "code"),
                memory_query_hints=("commit preferences",),
                activation_threshold=0.0,
            )
        )
        with_affordance = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="commit code")))

        self.assertEqual(with_affordance.candidates[0].memory_query_hints, ("commit preferences",))

    def test_behavior_advise_tool_is_async_first_and_falls_back_when_router_fails(self) -> None:
        async def failing_router(**kwargs):
            _ = kwargs
            raise TimeoutError("router down")

        service = BehaviorService(repository=self.repository, execution_runtime=self.runtime, semantic_router=failing_router)
        service.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="tool.route",
                module_id="test",
                title="Tool route",
                scenario_text="route me",
                prompt_hint="Route safely.",
                activation_terms=("route",),
                activation_threshold=0.0,
            )
        )
        tool = BehaviorAdviceTool(service=service)

        sync_result = tool.invoke({"scenario": "route me"})
        async_result = asyncio.run(tool.ainvoke({"scenario": "route me"}))

        self.assertEqual(sync_result.structured["reason"], "async_required")
        self.assertTrue(async_result.structured["fallback_used"])
        self.assertEqual(async_result.structured["candidates"][0]["affordance_id"], "tool.route")

    def test_skill_inject_returns_manual_without_executing_capability(self) -> None:
        self.repository.upsert_skill(
            SkillDescriptor(
                skill_id="commit",
                module_id="test",
                title="Commit",
                summary="Commit safely.",
                manual_text="1. Review changes.\n2. Commit.",
                capability_refs=("cap.known",),
            )
        )
        result = SkillInjectTool(service=self.skill_service).invoke({"skill_id": "commit"})

        self.assertEqual(result.structured["manual_text"], "1. Review changes.\n2. Commit.")
        self.assertEqual(result.structured["capability_refs"], ["cap.known"])
        self.assertEqual(result.status, "ok")

    def test_submit_affordance_tool_persists_instructed_route(self) -> None:
        result = AffordanceSubmitTool(service=self.service).invoke(
            {
                "scenario_text": "user says remember to show oled mood",
                "prompt_hint": "Use the OLED mood capability when Pal mood changes.",
                "activation_terms": ["oled", "mood"],
                "capability_refs": ["cap.known"],
            }
        )

        self.assertEqual(result.status, "ok")
        affordance_id = result.structured["affordance_id"]
        stored = self.repository.get_affordance(affordance_id)
        advice = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="oled mood changed")))

        self.assertIsNotNone(stored)
        self.assertEqual(stored.source_kind, AFFORDANCE_SOURCE_INSTRUCTED)
        self.assertEqual(advice.candidates[0].affordance_id, affordance_id)
        self.assertEqual(advice.candidates[0].availability, AFFORDANCE_AVAILABLE)

    def test_behavior_prompt_always_mentions_advise_inject_and_submit_tools(self) -> None:
        fragments = BehaviorPromptFragmentProvider(service=self.service).build_prompt_fragments(PromptAssemblyContext())
        content = "\n".join(fragment.content for fragment in fragments)

        self.assertIn("op_behavior_advise", content)
        self.assertIn("Advisor Gate", content)
        self.assertIn("op_exec_disc_search", content)
        self.assertIn("op_skill_inject", content)
        self.assertIn("op_behavior_affordance_submit", content)
        self.assertIn("memory_query_hints", content)
        self.assertNotIn("op_l3_recall_query", content)
        self.assertNotIn("op_l3_commit_write", content)

    def test_behavior_prompt_sections_enter_system_prompt_in_order(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_behavior_with_core(core.context, self.service)

        prompt = core.build_canonical_prompt(PromptAssemblyContext())
        system = prompt.messages[0]["content"]

        self.assertIn("## System Surfaces", system)
        self.assertIn("## Operating Rules", system)
        self.assertIn("## Behavior Routing", system)
        self.assertNotIn("## Memory Routing", system)
        self.assertNotIn("## Resident Affordances", system)
        self.assertLess(system.index("## System Surfaces"), system.index("## Operating Rules"))
        self.assertLess(system.index("## Operating Rules"), system.index("## Behavior Routing"))
        self.assertEqual(
            prompt.metadata["fragment_sections"],
            ["system_surfaces", "operating_rules", "behavior_routing"],
        )

        surfaces = system.split("## Operating Rules", 1)[0]
        self.assertIn('Capability answers: "What executable ability exists right now?"', surfaces)
        self.assertIn('Affordance answers: "When this kind of situation appears, what route should Pal consider?"', surfaces)
        self.assertIn('Skill answers: "What reusable procedure should Pal follow to accomplish this kind of task?"', surfaces)
        self.assertIn('Memory answers: "What durable fact, preference, history, or lesson may matter now?"', surfaces)
        operating = system.split("## Operating Rules", 1)[1].split("## Behavior Routing", 1)[0]
        self.assertIn("Source-of-Truth Preference", operating)
        self.assertIn("Mutation and Side-Effect Boundary", operating)
        self.assertIn("Priority", operating)
        self.assertNotIn("op_behavior_advise", operating)
        self.assertNotIn("op_l3_recall_query", operating)
        self.assertNotIn("op_l3_commit_write", operating)
        self.assertIn("Advisor Gate", system)
        self.assertIn("If the user teaches a future behavior rule, submit an affordance", system)

    def test_behavior_advice_tool_result_projects_to_behavior_guidance(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="commit.guidance",
                module_id="test",
                title="Commit guidance",
                scenario_text="commit code",
                prompt_hint="Consider checking the commit workflow before changing git state.",
                activation_terms=("commit", "code"),
                skill_refs=("commit.skill",),
                capability_refs=("cap.known",),
                memory_query_hints=("commit preferences",),
                activation_threshold=0.0,
            )
        )
        self.repository.upsert_skill(
            SkillDescriptor(
                skill_id="commit.skill",
                module_id="test",
                title="Commit skill",
                summary="Commit safely.",
                manual_text="Review changes, then commit.",
                capability_refs=("cap.known",),
            )
        )
        core = PalCore()
        register_core_with_core(core)
        register_behavior_with_core(core.context, self.service)
        register_channel_with_core(core.context, ChannelRuntime())
        memory_service = MemoryService()
        register_memory_with_core(core.context, memory_service)
        scripted_llm = _ScriptedLLMRuntime(
            [
                CanonicalLLMOutcome(
                    text="",
                    tool_calls=[CanonicalToolCall(name="op_behavior_advise", args={"scenario": "commit code"})],
                    finish_reason="tool_calls",
                ),
                CanonicalLLMOutcome(text="final answer", tool_calls=[], finish_reason="stop"),
            ]
        )
        core.context.port_registry["llm:llm"] = scripted_llm

        core.process_channel_turn(
            ChannelEnvelope(
                event=EventEnvelope(event_kind="user.message", source_kind="channel", payload={"text": "commit code"}),
                endpoint=EndpointConfig(endpoint_id="stdio", channel_kind="stdio", binding_key="stdin"),
                response_handle=ResponseHandle(endpoint_id="stdio"),
            )
        )

        entry = memory_service.l2_store.get_entry("behavior_advice:commit.guidance")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.kind, "behavior_rule")
        self.assertEqual(entry.source_kind, "behavior_advice")
        self.assertEqual(entry.candidate_state, "active")
        self.assertIn("op_skill_inject", entry.rendered)
        self.assertIn("commit preferences", entry.rendered)

        generate_requests = [request for kind, request in scripted_llm.requests if kind == "generate"]
        self.assertGreaterEqual(len(generate_requests), 2)
        followup_system = generate_requests[1].messages[0]["content"]
        self.assertIn("## Behavior Guidance", followup_system)
        self.assertIn("current-task behavior routing hints, not durable facts", followup_system)
        self.assertIn("Commit guidance", followup_system)

    def test_behavior_guidance_renders_separately_from_working_memory(self) -> None:
        pack = MemoryPack(
            l2_working_memory=[
                L2Entry(
                    entry_id="fact.timezone",
                    kind="fact",
                    scope="user",
                    title="Timezone Preference",
                    summary="User prefers Asia/Hong_Kong timezone.",
                    source_kind="l3_recall",
                    rendered="User prefers Asia/Hong_Kong timezone.",
                ),
                L2Entry(
                    entry_id="case.plugin",
                    kind="case",
                    scope="system",
                    title="Plugin repair",
                    summary="A prior plugin attach failure was fixed by rescanning.",
                    source_kind="l3_recall",
                    rendered="A prior plugin attach failure was fixed by rescanning.",
                ),
                L2Entry(
                    entry_id="behavior_advice:commit.guidance",
                    kind="behavior_rule",
                    scope="behavior",
                    title="Commit guidance",
                    summary="Consider checking the commit workflow.",
                    source_kind="behavior_advice",
                    rendered="Hint: Consider checking the commit workflow.",
                ),
            ]
        )

        fragments = MemoryPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext(metadata={"memory_pack": pack}))
        by_title = {fragment.title: fragment.content for fragment in fragments}

        self.assertIn("Remembered Facts", by_title)
        self.assertIn("Relevant Experience", by_title)
        self.assertIn("Behavior Guidance", by_title)
        self.assertNotIn("Working Memory", by_title)
        self.assertIn("recalled durable facts", by_title["Remembered Facts"])
        self.assertIn("Timezone Preference", by_title["Remembered Facts"])
        self.assertNotIn("Plugin repair", by_title["Remembered Facts"])
        self.assertIn("prior cases or lessons", by_title["Relevant Experience"])
        self.assertIn("Plugin repair", by_title["Relevant Experience"])
        self.assertNotIn("Timezone Preference", by_title["Relevant Experience"])
        self.assertNotIn("Commit guidance", by_title["Remembered Facts"])
        self.assertNotIn("Commit guidance", by_title["Relevant Experience"])
        self.assertIn("Commit guidance", by_title["Behavior Guidance"])
        self.assertIn("not durable facts", by_title["Behavior Guidance"])
        self.assertNotIn("origin available", by_title["Remembered Facts"])
        self.assertNotIn("origin available", by_title["Relevant Experience"])
        self.assertNotIn("origin available", by_title["Behavior Guidance"])

    def test_memory_prompt_always_projects_memory_routing(self) -> None:
        fragments = MemoryPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext())
        self.assertEqual([fragment.section for fragment in fragments], ["memory_routing"])
        routing = fragments[0].content

        self.assertIn("op_l3_recall_query", routing)
        self.assertIn("op_l3_commit_write", routing)
        self.assertIn("op_l3_correct_patch", routing)
        self.assertIn("memory_query_hints", routing)
        self.assertIn("approved repair lessons", routing)
        self.assertIn("If memory has been recalled or is present in the prompt", routing)
        self.assertIn("blocker, ambiguity, missing user/project context", routing)
        self.assertIn("If a tool/capability call fails", routing)
        self.assertIn("MUST use `op_l3_recall_query`", routing)
        self.assertIn("challenges Pal's memory", routing)
        self.assertIn("MUST recall relevant memory", routing)
        self.assertIn("custom term", routing)
        self.assertIn("Do not recall memory automatically for every task or every unknown.", routing)

    def test_behavior_guidance_l2_entries_do_not_retire_to_l3(self) -> None:
        service = MemoryService()
        service.l2_store.capacity = 0

        service.project_l2_entries(
            [
                L2Entry(
                    entry_id="behavior_advice:evict.me",
                    kind="behavior_rule",
                    scope="behavior",
                    title="Evict me",
                    summary="Temporary behavior guidance.",
                    source_kind="behavior_advice",
                    candidate_state="stable",
                )
            ],
            touch=True,
            top_of_mind=True,
        )

        self.assertIsNone(service.l2_store.get_entry("behavior_advice:evict.me"))
        self.assertEqual(service.failed_retirements, [])

    def test_resident_affordances_are_dynamic_system_prompt_blocks(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_behavior_with_core(core.context, self.service)

        without_resident = core.build_canonical_prompt(PromptAssemblyContext()).messages[0]["content"]
        self.assertNotIn("## Resident Affordances", without_resident)

        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="resident.oled",
                module_id="test",
                title="OLED expression",
                scenario_text="visible mood changed",
                prompt_hint="Consider updating the OLED expression when visible mood changes.",
                visibility_mode=AFFORDANCE_VISIBILITY_RESIDENT,
                activation_kind=AFFORDANCE_ACTIVATION_DELIBERATIVE,
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
            )
        )

        with_resident_prompt = core.build_canonical_prompt(PromptAssemblyContext())
        with_resident = with_resident_prompt.messages[0]["content"]

        self.assertIn("## Resident Affordances", with_resident)
        self.assertIn("OLED expression", with_resident)
        self.assertIn("Consider updating the OLED expression", with_resident)
        self.assertEqual(
            with_resident_prompt.metadata["fragment_sections"],
            ["system_surfaces", "operating_rules", "behavior_routing", "resident_affordances"],
        )

    def test_module_capabilities_are_auto_declared_as_affordances(self) -> None:
        subtree = MountedSubtreeHandle(
            module_id="demo",
            descriptors=[
                CapabilityDescriptor(
                    name="intro_module_demo_show",
                    canonical_path="intro_module_demo_show",
                    family="introspection",
                    description="Show demo module state",
                    source="builtin:demo",
                    display_name="demo show",
                    module_id="demo",
                ),
                CapabilityDescriptor(
                    name="op_demo_run",
                    canonical_path="op_demo_run",
                    family="demo",
                    description="Run demo operation",
                    source="builtin:demo",
                    display_name="demo run",
                    module_id="demo",
                ),
            ],
        )
        provider = object()
        handle = _FakeHandle(module_id="demo", introspection_provider=provider)
        handle.mounted_subtree = subtree

        self.service.register_declared_module(handle)
        advice = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="show demo module state run demo operation", top_k=10)))
        refs = {ref for candidate in advice.candidates for ref in candidate.capability_refs}

        self.assertIn("intro_module_demo_show", refs)
        self.assertIn("op_demo_run", refs)

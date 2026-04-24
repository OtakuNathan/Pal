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
    SkillInjectTool,
    affordance,
    skill,
)
from pal.behavior.prompt import BehaviorPromptFragmentProvider
from pal.execution import CapabilityDescriptor
from pal.foundation import PalV2Database
from pal.memory.models import MemoryCaseModel
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


class BehaviorSubsystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_behavior_test_"))
        self.database = PalV2Database(self.root / "pal_behavior.sqlite3")
        self.database.initialize([BehaviorAffordanceModel, BehaviorSkillModel, MemoryCaseModel])
        self.repository = BehaviorRepository()
        self.runtime = _FakeExecutionRuntime(available={"cap.known"})
        self.service = BehaviorService(repository=self.repository, execution_runtime=self.runtime)

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
        tool = SkillInjectTool(service=self.service)
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

        self.assertEqual(missing.structured["reason"], "skill_not_found_or_disabled")
        self.assertEqual(disabled.structured["reason"], "skill_not_found_or_disabled")

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
        result = SkillInjectTool(service=self.service).invoke({"skill_id": "commit"})

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
        self.assertIn("op_skill_inject", content)
        self.assertIn("op_behavior_affordance_submit", content)

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

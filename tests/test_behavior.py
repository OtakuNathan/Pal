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
    AffordanceDeleteTool,
    AffordanceSubmitTool,
    AffordanceUpdateTool,
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
from pal.behavior.tools import (
    BEHAVIOR_ADVICE_DESCRIPTION,
    BEHAVIOR_LEARN_DESCRIPTION,
    BEHAVIOR_UPDATE_DESCRIPTION,
)
from pal.channel import ChannelEnvelope, ChannelRuntime, EndpointConfig, ResponseHandle, register_with_core as register_channel_with_core
from pal.core import PalCore, register_with_core as register_core_with_core
from pal.execution import CapabilityDescriptor, register_with_core as register_execution_with_core
from pal.foundation import EventEnvelope, HeatLevel, HeatPolicy, HeatStateMachine, HeatStateRegistry, PalV2Database
from pal.lsp.plugin import LspManagerPluginProvider
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


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, list):
        return "\n".join(str(part.get("text") or "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    return str(content or "")


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
                capability_refs=("advise_behavior",),
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

    def test_behavior_advice_requires_relevance_before_source_priority(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="mcp.manager",
                module_id="test",
                title="MCP manager",
                scenario_text="MCP sidecar attach rescan detach",
                prompt_hint="Use MCP manager capabilities.",
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                priority=100,
            )
        )
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="research.note",
                module_id="test",
                title="代码调研后自动写研究笔记",
                scenario_text="研究代码仓库后写研究笔记",
                prompt_hint="研究完成后写结构化研究笔记。",
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                priority=100,
            )
        )

        result = asyncio.run(
            self.service.advise_async(
                BehaviorAdviceRequest(scenario="帮我研究 flux_foundry 仓库 flow，并写研究笔记", top_k=10)
            )
        )
        ids = {candidate.affordance_id for candidate in result.candidates}

        self.assertIn("research.note", ids)
        self.assertNotIn("mcp.manager", ids)

    def test_behavior_advice_uses_jieba_terms_for_chinese_routes(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="reply.style.zh",
                module_id="test",
                title="中文回复风格",
                scenario_text="用户要求使用简洁中文回复",
                prompt_hint="优先用简洁中文回复。",
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                activation_terms=("简洁中文回复",),
                activation_threshold=0.2,
            )
        )

        result = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="请用简洁中文回复", top_k=5)))

        self.assertGreaterEqual(len(result.candidates), 1)
        self.assertEqual(result.candidates[0].affordance_id, "reply.style.zh")

    def test_behavior_advice_uses_fts_relevance_without_mcp_vision_special_case(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="mcp.vision",
                module_id="mcp",
                title="Understand technical diagram",
                scenario_text=(
                    "Analyze visual diagrams, screenshots, architecture drawings, technical images, "
                    "UI screenshots, code screenshots, error messages, and design diagrams."
                ),
                prompt_hint=(
                    "Consider vision tool for technical diagram analysis. "
                    "Do NOT use for ordinary code repository analysis."
                ),
                source_kind=AFFORDANCE_SOURCE_DECLARED,
                activation_terms=("analyze", "technical", "diagram", "screenshot", "image", "vision"),
                capability_refs=("mcp_zai_vision_tool_understand_technical_diagram",),
                activation_threshold=0.35,
                priority=55,
            )
        )
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="code.research",
                module_id="test",
                title="Code research note",
                scenario_text="Analyze code repository architecture flow implementation and write research notes.",
                prompt_hint="Use code research workflow after repository analysis.",
                activation_terms=("code", "repository", "architecture", "research", "flow"),
                activation_threshold=0.25,
                priority=100,
            )
        )

        code_result = asyncio.run(
            self.service.advise_async(
                BehaviorAdviceRequest(
                    scenario="Analyze the flux_foundry code repository, especially the flow architecture and design implementation.",
                    top_k=10,
                )
            )
        )
        code_ids = {candidate.affordance_id for candidate in code_result.candidates}
        self.assertIn("code.research", code_ids)
        self.assertNotIn("mcp.vision", code_ids)

        diagram_result = asyncio.run(
            self.service.advise_async(BehaviorAdviceRequest(scenario="Please analyze this screenshot technical diagram.", top_k=10))
        )
        diagram_ids = {candidate.affordance_id for candidate in diagram_result.candidates}
        self.assertIn("mcp.vision", diagram_ids)
        self.assertNotIn("code.research", diagram_ids)

    def test_affordance_heat_uses_shared_heat_state_machine(self) -> None:
        service = BehaviorService(
            repository=self.repository,
            execution_runtime=self.runtime,
            affordance_heat=HeatStateRegistry(machine=HeatStateMachine(HeatPolicy(hot_ttl=1, ghost_ttl=1))),
        )
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="hot.route",
                module_id="test",
                title="Hot route",
                scenario_text="stabilize telegram routing",
                prompt_hint="Use the hot route.",
                activation_terms=("stabilize", "telegram", "routing"),
                activation_threshold=0.0,
            )
        )

        result = asyncio.run(service.advise_async(BehaviorAdviceRequest(scenario="stabilize telegram routing")))

        self.assertEqual(result.candidates[0].affordance_id, "hot.route")
        self.assertEqual(service.hot_affordance_ids(), ("hot.route",))
        self.assertEqual(service.hot_affordances()[0].affordance_id, "hot.route")

        self.assertEqual(service.tick_affordance_heat(), ())
        self.assertEqual(service.affordance_heat.get("hot.route").heat_level, HeatLevel.GHOST)
        self.assertEqual(service.tick_affordance_heat(), ("hot.route",))
        self.assertIsNone(service.affordance_heat.get("hot.route"))

    def test_advisor_hints_have_behavior_owned_capacity_and_heat_lifecycle(self) -> None:
        service = BehaviorService(
            repository=self.repository,
            execution_runtime=self.runtime,
            advisor_hint_capacity=1,
            advisor_hint_heat=HeatStateRegistry(machine=HeatStateMachine(HeatPolicy(hot_ttl=1, ghost_ttl=1))),
        )
        for affordance_id, title in (("hot.route.a", "Hot route A"), ("hot.route.b", "Hot route B")):
            self.repository.upsert_affordance(
                AffordanceDescriptor(
                    affordance_id=affordance_id,
                    module_id="test",
                    title=title,
                    scenario_text="stabilize telegram routing",
                    prompt_hint=f"Use {title}.",
                    activation_terms=("stabilize", "telegram", "routing"),
                    activation_threshold=0.0,
                )
            )

        asyncio.run(service.advise_async(BehaviorAdviceRequest(scenario="stabilize telegram routing", top_k=2)))

        self.assertEqual(len(service.advisor_hints), 1)
        self.assertEqual(len(service.active_advisor_hints()), 1)
        self.assertEqual(service.tick_advisor_hints(), ())
        self.assertEqual(service.active_advisor_hints(), ())
        expired = service.tick_advisor_hints()
        self.assertEqual(len(expired), 1)
        self.assertEqual(service.advisor_hints, {})

    def test_behavior_fts_index_updates_and_declared_delete_removes_stale_hits(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="declared.stale",
                module_id="demo",
                title="Old route",
                scenario_text="obsolete frobnicate path",
                prompt_hint="Old hint.",
                source_kind=AFFORDANCE_SOURCE_DECLARED,
                activation_terms=("obsolete", "frobnicate"),
                activation_threshold=0.0,
            )
        )
        old_result = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="obsolete frobnicate path")))
        self.assertEqual(old_result.candidates[0].affordance_id, "declared.stale")

        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="declared.stale",
                module_id="demo",
                title="New route",
                scenario_text="fresh replacement route",
                prompt_hint="New hint.",
                source_kind=AFFORDANCE_SOURCE_DECLARED,
                activation_terms=("fresh", "replacement"),
                activation_threshold=0.0,
            )
        )
        after_update_old = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="obsolete frobnicate path")))
        after_update_new = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="fresh replacement route")))
        self.assertEqual(after_update_old.candidates, ())
        self.assertEqual(after_update_new.candidates[0].affordance_id, "declared.stale")

        self.service.unregister_declared_module("demo")
        after_delete = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="fresh replacement route")))
        self.assertEqual(after_delete.candidates, ())

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

        self.assertIsNone(self.repository.get_affordance("declared.affordance"))
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
        reminder = str(prompt.messages[-1]["content"])

        self.assertNotIn("\n<behavior_guidance>\n", system)
        self.assertIn("<behavior_guidance>", reminder)
        self.assertIn("Declared resident", reminder)
        self.assertIn("Consider declared resident guidance.", reminder)
        self.assertNotIn("resident_affordances", prompt.metadata["fragment_sections"])
        self.assertIn("resident_affordances", prompt.metadata["reminder_sections"])

        self.service.unregister_declared_module("declared_resident_plugin")
        after_prompt = core.build_canonical_prompt(PromptAssemblyContext())
        after = "\n".join(str(message["content"]) for message in after_prompt.messages)
        self.assertNotIn("Declared resident", after)

    def test_lsp_provider_declares_resident_code_intelligence_affordance(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_behavior_with_core(core.context, self.service)

        provider = LspManagerPluginProvider(runtime_root=self.root)
        handle = _FakeHandle(module_id="lsp", introspection_provider=provider)
        self.service.register_declared_module(handle)

        self.assertIsNone(self.repository.get_affordance("declared.lsp.code_intelligence"))
        prompt = core.build_canonical_prompt(PromptAssemblyContext())
        system = prompt.messages[0]["content"]
        reminder = str(prompt.messages[-1]["content"])
        guidance = reminder.split("<behavior_guidance>", 1)[1].split("</behavior_guidance>", 1)[0]

        self.assertNotIn("\n<behavior_guidance>\n", system)
        self.assertIn("LSP code intelligence", guidance)
        self.assertIn("call lsp_prepare_workspace once before using LSP code intelligence", guidance)
        self.assertIn("lsp_document_symbols/workspace_symbols", guidance)
        self.assertIn("lsp_diagnostics after edits", guidance)
        self.assertIn("resident_affordances", prompt.metadata["reminder_sections"])

    def test_declared_affordance_canonicalizes_prompt_hint_title_prefix(self) -> None:
        @affordance(
            affordance_id="declared.clean",
            title="Declared clean",
            scenario_text="declared clean scenario",
            prompt_hint="Declared clean: Use declared clean guidance.",
            activation_terms=("declared-clean",),
            activation_threshold=0.0,
        )
        class DeclaredProvider:
            module_id = "declared_plugin"

        handle = _FakeHandle(module_id="declared_plugin", introspection_provider=DeclaredProvider())
        self.service.register_declared_module(handle)

        result = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="declared-clean", top_k=5)))

        self.assertEqual(result.candidates[0].prompt_hint, "Use declared clean guidance.")

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
        self.assertIn("Route safely.", async_result.llm_text)
        self.assertNotIn("confidence", async_result.llm_text)
        self.assertNotIn("lexical score", async_result.llm_text)
        self.assertNotIn("source_kind", async_result.llm_text)
        self.assertNotIn("metadata", async_result.llm_text)

    def test_semantic_router_cannot_drop_deterministic_candidates(self) -> None:
        def biased_router(**kwargs):
            candidates = tuple(kwargs["candidates"])
            return tuple(candidate for candidate in candidates if candidate.affordance_id == "minion.route")

        service = BehaviorService(repository=self.repository, execution_runtime=self.runtime, semantic_router=biased_router)
        service.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="proactive.route",
                module_id="test",
                title="Proactive schedule route",
                scenario_text="scheduled recurring daily push notifications",
                prompt_hint="Use Proactive for scheduled recurring push work.",
                activation_terms=("schedule", "daily", "push"),
                activation_threshold=0.0,
            )
        )
        service.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="minion.route",
                module_id="test",
                title="Minion route",
                scenario_text="schedule daily push delegated to minion",
                prompt_hint="Consider Minion for delegated work.",
                activation_terms=("schedule", "daily", "push", "delegate", "minion"),
                activation_threshold=0.0,
            )
        )

        result = asyncio.run(service.advise_async(BehaviorAdviceRequest(scenario="schedule daily push", top_k=5)))
        ids = [candidate.affordance_id for candidate in result.candidates]

        self.assertIn("minion.route", ids)
        self.assertIn("proactive.route", ids)

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

    def test_submit_affordance_tool_canonicalizes_prompt_hint_title_prefix(self) -> None:
        result = AffordanceSubmitTool(service=self.service).invoke(
            {
                "title": "Task routing",
                "scenario_text": "task routing scenario",
                "prompt_hint": "Task routing: Handle chat directly and delegate concrete implementation work.",
                "activation_terms": ["task-routing"],
            }
        )
        stored = self.repository.get_affordance(result.structured["affordance_id"])

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.structured["prompt_hint"], "Handle chat directly and delegate concrete implementation work.")
        self.assertEqual(stored.prompt_hint, "Handle chat directly and delegate concrete implementation work.")

    def test_learn_behavior_conflict_requires_user_decision_by_default(self) -> None:
        first = AffordanceSubmitTool(service=self.service).invoke(
            {
                "scenario_text": "user asks for a careful commit",
                "prompt_hint": "Inspect git status before committing.",
            }
        )
        second = AffordanceSubmitTool(service=self.service).invoke(
            {
                "scenario_text": " user   asks for a careful commit ",
                "prompt_hint": "Run tests before committing.",
            }
        )

        self.assertEqual(first.status, "ok")
        self.assertEqual(second.status, "invalid")
        self.assertEqual(second.structured["reason"], "behavior_learn_conflict")
        self.assertEqual(second.structured["action_required"], "ask_user")
        self.assertEqual(second.structured["conflict_resolution_options"], ["merge", "overwrite", "skip"])
        self.assertEqual(second.structured["candidates"][0]["affordance_id"], first.structured["affordance_id"])

    def test_learn_behavior_merge_combines_same_scenario_when_explicit(self) -> None:
        first = AffordanceSubmitTool(service=self.service).invoke(
            {
                "scenario_text": "user asks for a careful commit",
                "prompt_hint": "Inspect git status before committing.",
                "activation_terms": ["commit"],
            }
        )
        merged = AffordanceSubmitTool(service=self.service).invoke(
            {
                "scenario_text": "user asks for a careful commit",
                "prompt_hint": "Run tests before committing.",
                "activation_terms": ["tests"],
                "conflict_resolution": "merge",
            }
        )
        stored = self.repository.get_affordance(first.structured["affordance_id"])

        self.assertEqual(merged.status, "ok")
        self.assertEqual(merged.structured["learn_result"], "merged")
        self.assertEqual(merged.structured["affordance_id"], first.structured["affordance_id"])
        self.assertIn("Inspect git status before committing.", stored.prompt_hint)
        self.assertIn("Run tests before committing.", stored.prompt_hint)
        self.assertEqual(stored.activation_terms, ("commit", "tests"))

    def test_learn_behavior_overwrite_replaces_same_scenario_when_explicit(self) -> None:
        first = AffordanceSubmitTool(service=self.service).invoke(
            {
                "scenario_text": "user asks for a careful commit",
                "prompt_hint": "Inspect git status before committing.",
                "activation_terms": ["commit"],
            }
        )
        overwritten = AffordanceSubmitTool(service=self.service).invoke(
            {
                "scenario_text": "user asks for a careful commit",
                "prompt_hint": "Run focused tests before committing.",
                "activation_terms": ["tests"],
                "conflict_resolution": "overwrite",
            }
        )
        stored = self.repository.get_affordance(first.structured["affordance_id"])

        self.assertEqual(overwritten.status, "ok")
        self.assertEqual(overwritten.structured["learn_result"], "overwritten")
        self.assertEqual(overwritten.structured["affordance_id"], first.structured["affordance_id"])
        self.assertEqual(stored.prompt_hint, "Run focused tests before committing.")
        self.assertEqual(stored.activation_terms, ("tests",))

    def test_learn_behavior_skip_keeps_same_scenario_when_explicit(self) -> None:
        first = AffordanceSubmitTool(service=self.service).invoke(
            {
                "scenario_text": "user asks for a careful commit",
                "prompt_hint": "Inspect git status before committing.",
                "activation_terms": ["commit"],
            }
        )
        skipped = AffordanceSubmitTool(service=self.service).invoke(
            {
                "scenario_text": "user asks for a careful commit",
                "prompt_hint": "Run focused tests before committing.",
                "activation_terms": ["tests"],
                "conflict_resolution": "skip",
            }
        )
        stored = self.repository.get_affordance(first.structured["affordance_id"])

        self.assertEqual(skipped.status, "ok")
        self.assertEqual(skipped.structured["learn_result"], "skipped")
        self.assertEqual(skipped.structured["affordance_id"], first.structured["affordance_id"])
        self.assertEqual(stored.prompt_hint, "Inspect git status before committing.")
        self.assertEqual(stored.activation_terms, ("commit",))

    def test_read_tool_contracts_separate_memory_from_behavior(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_execution_with_core(core.context)
        register_memory_with_core(core.context, MemoryService())
        register_behavior_with_core(core.context, self.service)
        for module_id in ("execution", "memory", "behavior"):
            core.publish_module_capabilities(module_id)

        exposed_names = [
            item["function"]["name"]
            for item in core.tool_surface.build_llm_tool_contracts()
        ]
        self.assertIn("learn_behavior", exposed_names)
        self.assertIn("update_behavior", exposed_names)
        self.assertIn("forget_behavior", exposed_names)
        self.assertIn("remember_memory", exposed_names)
        self.assertIn("forget_memory", exposed_names)
        self.assertNotIn("save_behavior", exposed_names)
        self.assertNotIn("write_memory", exposed_names)
        self.assertNotIn("delete_memory", exposed_names)
        learn_tool = next(item for item in core.tool_surface.build_llm_tool_contracts() if item["function"]["name"] == "learn_behavior")
        learn_properties = learn_tool["function"]["input_schema"]["properties"]
        self.assertIn("resident", learn_properties)
        self.assertNotIn("visibility_mode", learn_properties)
        self.assertNotIn("activation_kind", learn_properties)
        self.assertNotIn("activation_mode", learn_properties)
        self.assertNotIn("source_kind", learn_properties)
        self.assertNotIn("priority", learn_properties)
        self.assertNotIn("activation_threshold", learn_properties)

        def description(name: str) -> str:
            result = core.context.execution_runtime.execute_tool(CanonicalToolCall(name="read_tool", args={"name": name}))
            self.assertTrue(result.ok, result.text)
            return str(result.structured["description"])

        recall_description = description("recall_memory")
        remember_description = description("remember_memory")
        learn_description = description("learn_behavior")
        self.assertIn("Memory is Pal's remembered facts", recall_description)
        self.assertIn("kind='case'", recall_description)
        self.assertIn("prior failures and fixes", recall_description)
        self.assertNotIn("behavior guidance, or skills", recall_description)
        self.assertIn("Do not use for behavior rules", remember_description)
        self.assertIn("condition-reflex layer", learn_description)
        self.assertIn("use remember_memory for facts", learn_description)

    def test_update_affordance_tool_preserves_source_metadata_and_refs(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="skill.route.debug",
                module_id="skill",
                title="Debug skill route",
                scenario_text="debug async failure",
                prompt_hint="Use the old debugging hint.",
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                skill_refs=("debug.skill",),
                capability_refs=("cap.known",),
                evidence_refs=("skill:debug.skill",),
                metadata={"generated_by": "op_skill_commit"},
                activation_threshold=0.0,
            )
        )

        result = AffordanceUpdateTool(service=self.service).invoke(
            {
                "affordance": "Use the old debugging hint.",
                "prompt_hint": "Use the updated debugging hint.",
                "activation_terms": ["updated-debug"],
            }
        )
        stored = self.repository.get_affordance("skill.route.debug")
        advice = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="updated-debug problem", top_k=5)))

        self.assertEqual(result.status, "ok")
        self.assertEqual(stored.module_id, "skill")
        self.assertEqual(stored.prompt_hint, "Use the updated debugging hint.")
        self.assertEqual(stored.skill_refs, ("debug.skill",))
        self.assertEqual(stored.capability_refs, ("cap.known",))
        self.assertEqual(stored.evidence_refs, ("skill:debug.skill",))
        self.assertEqual(stored.metadata, {"generated_by": "op_skill_commit"})
        self.assertEqual(advice.candidates[0].affordance_id, "skill.route.debug")

    def test_update_affordance_tool_canonicalizes_prompt_hint_title_prefix(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="task.routing",
                module_id="behavior",
                title="Task routing",
                scenario_text="task routing scenario",
                prompt_hint="Old task routing hint.",
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                activation_threshold=0.0,
            )
        )

        result = AffordanceUpdateTool(service=self.service).invoke(
            {
                "affordance": "Old task routing hint.",
                "prompt_hint": "Task routing: Handle chat directly and delegate concrete implementation work.",
            }
        )
        stored = self.repository.get_affordance("task.routing")

        self.assertEqual(result.status, "ok")
        self.assertEqual(stored.prompt_hint, "Handle chat directly and delegate concrete implementation work.")

    def test_update_affordance_tool_rejects_declared_injected_guidance(self) -> None:
        descriptor = AffordanceDescriptor(
            affordance_id="declared.injected.route",
            module_id="plugin.test",
            title="Injected route",
            scenario_text="plugin injected behavior",
            prompt_hint="Use injected plugin route.",
            source_kind=AFFORDANCE_SOURCE_DECLARED,
            activation_threshold=0.0,
        )
        self.service.declared_affordances["plugin.test"] = (descriptor,)

        result = AffordanceUpdateTool(service=self.service).invoke(
            {
                "affordance": "Use injected plugin route.",
                "prompt_hint": "Try to rewrite plugin guidance.",
            }
        )

        self.assertEqual(result.status, "invalid")
        self.assertIn("readonly injected affordance", result.structured["error"])

    def test_update_affordance_tool_matches_rendered_resident_guidance_line(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="rendered.line.route",
                module_id="behavior",
                title="Minion skill dispatch",
                scenario_text="dispatching minion tasks",
                prompt_hint="Before dispatching a minion, search the skill library for matching skills and inject them first.",
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                activation_threshold=0.0,
            )
        )

        result = AffordanceUpdateTool(service=self.service).invoke(
            {
                "affordance": "- Minion skill dispatch: Before dispatching a minion, search the skill library for matching skills and inject them first.",
                "prompt_hint": "Before dispatching minion tasks, inject matching skills first.",
            }
        )
        stored = self.repository.get_affordance("rendered.line.route")

        self.assertEqual(result.status, "ok")
        self.assertEqual(stored.prompt_hint, "Before dispatching minion tasks, inject matching skills first.")

    def test_update_affordance_tool_matches_rendered_guidance_with_colon_title(self) -> None:
        title = "Task routing: handle social/simple work directly, delegate implementation"
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="rendered.colon.title",
                module_id="behavior",
                title=title,
                scenario_text="task routing",
                prompt_hint="Handle chat directly and delegate concrete implementation work.",
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                activation_threshold=0.0,
            )
        )

        result = AffordanceUpdateTool(service=self.service).invoke(
            {
                "affordance": f"- {title}: Handle chat directly and delegate concrete implementation work.",
                "prompt_hint": "Use the revised task routing hint.",
            }
        )
        stored = self.repository.get_affordance("rendered.colon.title")

        self.assertEqual(result.status, "ok")
        self.assertEqual(stored.prompt_hint, "Use the revised task routing hint.")

    def test_update_affordance_tool_matches_xml_wrapped_guidance(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="xml.route",
                module_id="behavior",
                title="XML route",
                scenario_text="xml guidance",
                prompt_hint="Use XML wrapped affordance text.",
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                activation_threshold=0.0,
            )
        )

        result = AffordanceUpdateTool(service=self.service).invoke(
            {
                "affordance": "<behavior_guidance>\n- XML route: Use XML wrapped affordance text.\n</behavior_guidance>",
                "prompt_hint": "Updated XML wrapped affordance text.",
            }
        )
        stored = self.repository.get_affordance("xml.route")

        self.assertEqual(result.status, "ok")
        self.assertEqual(stored.prompt_hint, "Updated XML wrapped affordance text.")

    def test_delete_affordance_tool_matches_text_and_deletes_database_guidance(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="delete.route",
                module_id="behavior",
                title="Delete route",
                scenario_text="remove this behavior route",
                prompt_hint="Delete me by original text.",
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                activation_threshold=0.0,
            )
        )

        result = AffordanceDeleteTool(service=self.service).invoke({"affordance": "Delete me by original text."})

        self.assertEqual(result.status, "ok")
        self.assertTrue(result.structured["deleted"])
        self.assertIsNone(self.repository.get_affordance("delete.route"))

    def test_update_affordance_tool_returns_ambiguous_for_multiple_text_matches(self) -> None:
        for suffix in ("a", "b"):
            self.repository.upsert_affordance(
                AffordanceDescriptor(
                    affordance_id=f"duplicate.route.{suffix}",
                    module_id="behavior",
                    title=f"Duplicate {suffix}",
                    scenario_text=f"duplicate route {suffix}",
                    prompt_hint="Shared duplicate guidance.",
                    source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                    activation_threshold=0.0,
                )
            )

        result = AffordanceUpdateTool(service=self.service).invoke(
            {
                "affordance": "Shared duplicate guidance.",
                "prompt_hint": "Updated duplicate guidance.",
            }
        )

        self.assertEqual(result.status, "invalid")
        self.assertIn("matched multiple entries", result.structured["error"])

    def test_behavior_advise_does_not_canonicalize_dirty_stored_prompt_hint(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="dirty.route",
                module_id="behavior",
                title="Dirty route",
                scenario_text="dirty route scenario",
                prompt_hint="Dirty route: Use the clean hint.",
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
                activation_terms=("dirty-route",),
                activation_threshold=0.0,
            )
        )

        result = asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="dirty-route", top_k=5)))

        self.assertEqual(result.candidates[0].prompt_hint, "Dirty route: Use the clean hint.")

    def test_behavior_prompt_leaves_static_routing_to_tool_descriptions(self) -> None:
        fragments = BehaviorPromptFragmentProvider(service=self.service).build_prompt_fragments(PromptAssemblyContext())
        content = "\n".join(fragment.content for fragment in fragments)

        self.assertIn("Behavior guidance answers", content)
        self.assertIn("future routing rules and recurring decision hints", content)
        self.assertIn("Memory answers", content)
        self.assertIn("Use memory for remembered facts and reusable case knowledge", content)
        self.assertIn("Use the skill system for reusable procedures/playbooks", content)
        self.assertIn("Behavior tools define advice, learn, update", content)
        self.assertNotIn("behavior_advise", content)
        self.assertNotIn("save_behavior", content)
        self.assertNotIn("behavior_affordance_update", content)
        self.assertNotIn("If advice returns `skill_ref`, call `skill_inject` before executing that workflow", content)
        self.assertNotIn("affordances with affordance_id values", content)
        self.assertNotIn("op_memory_write", content)

        advice_description = BEHAVIOR_ADVICE_DESCRIPTION
        self.assertIn("condition-reflex layer", advice_description)
        self.assertIn("ambiguous, risky, multi-step", advice_description)
        self.assertIn("clear direct implementation command", advice_description)
        self.assertIn("Treat the result as routing resources, not orders", advice_description)
        save_description = BEHAVIOR_LEARN_DESCRIPTION
        self.assertIn("Learn a future behavior rule", save_description)
        self.assertIn("use remember_memory for facts", save_description)
        update_description = BEHAVIOR_UPDATE_DESCRIPTION
        self.assertIn("Pass the original rendered guidance line", update_description)
        self.assertIn("Do not claim behavior guidance changed unless this tool confirms success", update_description)

    def test_behavior_prompt_sections_enter_system_prompt_in_order(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_behavior_with_core(core.context, self.service)

        prompt = core.build_canonical_prompt(PromptAssemblyContext())
        system = prompt.messages[0]["content"]
        reminder = str(prompt.messages[-1]["content"])

        self.assertIn("<system_map>", system)
        self.assertIn("<source_of_truth>", system)
        self.assertIn("<prompt_context_policy>", system)
        self.assertIn("<operating_rules>", system)
        self.assertIn("<priority>", system)
        self.assertNotIn("<task_flow>", system)
        self.assertNotIn("<tool_efficiency>", system)
        self.assertIn("<mutation_policy>", system)
        self.assertIn("<behavior_guidance_guide>", system)
        self.assertIn("<knowledge_storage_boundary>", system)
        self.assertNotIn("<memory_guide>", system)
        self.assertNotIn("\n<behavior_guidance>\n", system)
        self.assertNotIn("##", system)
        self.assertLess(system.index("<system_map>"), system.index("<source_of_truth>"))
        self.assertLess(system.index("<source_of_truth>"), system.index("<prompt_context_policy>"))
        self.assertLess(system.index("<prompt_context_policy>"), system.index("<operating_rules>"))
        self.assertLess(system.index("<operating_rules>"), system.index("<priority>"))
        self.assertLess(system.index("<mutation_policy>"), system.index("<behavior_guidance_guide>"))
        self.assertEqual(
            prompt.metadata["fragment_sections"],
            [
                "system_map",
                "source_of_truth",
                "prompt_context_policy",
                "operating_rules",
                "priority",
                "mutation_policy",
                "behavior_guidance_guide",
                "knowledge_storage_boundary",
            ],
        )
        self.assertEqual(prompt.metadata["reminder_sections"], ["operating_guidance", "tool_efficiency"])

        surfaces = system.split("<operating_rules>", 1)[0]
        self.assertIn("execution/capability", surfaces)
        self.assertIn("behavior: behavior guidance", surfaces)
        self.assertNotIn("minion", system.split("</system_map>", 1)[0].lower())
        source_of_truth = system.split("<source_of_truth>", 1)[1].split("</source_of_truth>", 1)[0]
        self.assertIn("Use the right source for the truth needed", source_of_truth)
        self.assertIn("live introspection/capability calls", source_of_truth)
        operating = system.split("<operating_rules>", 1)[1].split("</operating_rules>", 1)[0]
        self.assertIn("No success claim without confirmation", operating)
        self.assertNotIn("op_behavior_advise", operating)
        self.assertNotIn("op_memory_recall", operating)
        self.assertNotIn("op_memory_write", operating)
        mutation = system.split("<mutation_policy>", 1)[1].split("</mutation_policy>", 1)[0]
        self.assertIn("Runtime capability calls are governed actions", mutation)
        self.assertIn("Source code, config, policy, and approval-boundary changes require explicit user request or approval", mutation)
        self.assertIn("bypassing capability policy", mutation)
        self.assertNotIn("<task_flow>", reminder)
        self.assertIn("<tool_efficiency>", reminder)
        self.assertIn("behavior-routing guidance", reminder)
        self.assertIn("active system prompt's hard rules", reminder)
        self.assertNotIn("behavior_advise", reminder)
        self.assertNotIn("save_behavior", system)
        self.assertIn("Stable fact, preference, project context, prior decision, or repair lesson -> memory", system)

    def test_behavior_advice_tool_result_activates_temporary_behavior_guidance(self) -> None:
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
        core.publish_module_capabilities("behavior")
        register_channel_with_core(core.context, ChannelRuntime())
        memory_service = MemoryService()
        register_memory_with_core(core.context, memory_service)
        scripted_llm = _ScriptedLLMRuntime(
            [
                CanonicalLLMOutcome(
                    text="",
                    tool_calls=[CanonicalToolCall(name="advise_behavior", args={"scenario": "commit code"})],
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

        self.assertIsNone(memory_service.l2_store.get_entry("behavior_advice:commit.guidance"))
        hints = self.service.active_advisor_hints()
        self.assertEqual([hint.hint_id for hint in hints], ["commit.guidance"])
        self.assertIn("skill_inject", hints[0].rendered)
        self.assertIn("MUST NOT call `skill_inject` solely because listed", hints[0].rendered)
        self.assertIn("commit preferences", hints[0].rendered)

        generate_requests = [request for kind, request in scripted_llm.requests if kind == "generate"]
        self.assertGreaterEqual(len(generate_requests), 2)
        followup_system = generate_requests[1].messages[0]["content"]
        self.assertNotIn("Active Route Guidance", followup_system)
        followup_text = "\n".join(_message_text(message) for message in generate_requests[1].messages)
        self.assertIn("Behavior advice", followup_text)
        self.assertIn("<behavior_guidance>", followup_text)
        self.assertNotIn("<advisor_hints>", followup_text)
        self.assertIn("Temporary behavior guidance from advise_behavior", followup_text)
        self.assertIn("Commit guidance", followup_text)
        self.assertIn("commit.skill", followup_text)
        self.assertIn("commit preferences", followup_text)
        self.assertNotIn("Route metadata", followup_text)
        self.assertNotIn("confidence=", followup_text)
        self.assertNotIn("lexical score", followup_text)

    def test_behavior_guidance_renders_from_behavior_not_working_memory(self) -> None:
        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="commit.guidance",
                module_id="test",
                title="Commit guidance",
                scenario_text="commit code",
                prompt_hint="Consider checking the commit workflow.",
                activation_terms=("commit", "code"),
                activation_threshold=0.0,
            )
        )
        asyncio.run(self.service.advise_async(BehaviorAdviceRequest(scenario="commit code")))
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

        memory_fragments = MemoryPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext(metadata={"memory_pack": pack}))
        memory_by_title = {fragment.title: fragment.content for fragment in memory_fragments}
        behavior_fragments = BehaviorPromptFragmentProvider(service=self.service).build_prompt_fragments(PromptAssemblyContext(metadata={"memory_pack": pack}))
        behavior_by_title = {fragment.title: fragment.content for fragment in behavior_fragments}

        self.assertIn("Recalled memories", memory_by_title)
        self.assertNotIn("Active Behavior Guidance", memory_by_title)
        self.assertNotIn("Working Memory", memory_by_title)
        self.assertIn('<recalled_memories view="summary">', memory_by_title["Recalled memories"])
        self.assertIn("</recalled_memories>", memory_by_title["Recalled memories"])
        self.assertIn("[fact.timezone]: User prefers Asia/Hong_Kong timezone.", memory_by_title["Recalled memories"])
        self.assertIn("[case.plugin]: A prior plugin attach failure was fixed by rescanning.", memory_by_title["Recalled memories"])
        self.assertNotIn("Timezone Preference", memory_by_title["Recalled memories"])
        self.assertNotIn("Plugin repair", memory_by_title["Recalled memories"])
        self.assertNotIn("Commit guidance", memory_by_title["Recalled memories"])
        self.assertNotIn("origin available", memory_by_title["Recalled memories"])
        self.assertIn("Active Behavior Guidance", behavior_by_title)
        self.assertNotIn("<advisor_hints>", behavior_by_title["Active Behavior Guidance"])
        self.assertIn("Temporary behavior guidance from advise_behavior", behavior_by_title["Active Behavior Guidance"])
        self.assertIn("Commit guidance", behavior_by_title["Active Behavior Guidance"])
        self.assertIn("Hint: Consider checking the commit workflow.", behavior_by_title["Active Behavior Guidance"])
        self.assertNotIn("origin available", behavior_by_title["Active Behavior Guidance"])

    def test_memory_prompt_leaves_static_routing_to_tool_descriptions(self) -> None:
        fragments = MemoryPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext())
        self.assertEqual([fragment.section for fragment in fragments], ["memory_guide"])
        policy = fragments[0].content

        self.assertIn("repair lessons", policy)
        self.assertIn("When work hits an error", policy)
        self.assertIn("kind=case", policy)
        self.assertIn('memory answers "what should Pal remember as true or reusable knowledge?"', policy)
        self.assertIn('Behavior guidance answers "when this situation appears, what route/action should Pal consider?"', policy)
        self.assertIn("use behavior guidance instead of memory", policy)
        self.assertIn("Reusable procedures/playbooks belong to the skill system", policy)
        self.assertIn("Memory tool descriptions", policy)
        self.assertIn("prefixes such as fact: and case:", policy)
        self.assertNotIn("memory_recall", policy)
        self.assertNotIn("memory_write", policy)
        self.assertNotIn("MUST call memory_recall", policy)

    def test_memory_projection_ignores_behavior_scoped_entries(self) -> None:
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
        self.assertNotIn("\n<behavior_guidance>\n", without_resident)

        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="resident.oled",
                module_id="test",
                title="OLED expression",
                scenario_text="visible mood changed",
                prompt_hint="Use the OLED expression capability sparingly when an expressive reaction naturally fits.",
                visibility_mode=AFFORDANCE_VISIBILITY_RESIDENT,
                activation_kind=AFFORDANCE_ACTIVATION_DELIBERATIVE,
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
            )
        )

        with_resident_prompt = core.build_canonical_prompt(PromptAssemblyContext())
        with_resident = with_resident_prompt.messages[0]["content"]
        reminder = str(with_resident_prompt.messages[-1]["content"])

        self.assertNotIn("\n<behavior_guidance>\n", with_resident)
        self.assertIn("<behavior_guidance>", reminder)
        self.assertIn("OLED expression", reminder)
        self.assertIn("Use the OLED expression capability sparingly", reminder)
        self.assertEqual(
            with_resident_prompt.metadata["fragment_sections"],
            [
                "system_map",
                "source_of_truth",
                "prompt_context_policy",
                "operating_rules",
                "priority",
                "mutation_policy",
                "behavior_guidance_guide",
                "knowledge_storage_boundary",
            ],
        )
        self.assertEqual(
            with_resident_prompt.metadata["reminder_sections"],
            ["operating_guidance", "resident_affordances", "tool_efficiency"],
        )

    def test_behavior_guidance_deduplicates_headers_and_uses_canonicalized_declared_titles(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        register_behavior_with_core(core.context, self.service)

        self.repository.upsert_affordance(
            AffordanceDescriptor(
                affordance_id="resident.oled",
                module_id="test",
                title="OLED expression",
                scenario_text="visible mood changed",
                prompt_hint="Use the OLED expression capability sparingly.",
                visibility_mode=AFFORDANCE_VISIBILITY_RESIDENT,
                activation_kind=AFFORDANCE_ACTIVATION_DELIBERATIVE,
                source_kind=AFFORDANCE_SOURCE_INSTRUCTED,
            )
        )

        @affordance(
            affordance_id="declared.task_routing",
            title="Task routing: handle social/simple work directly, delegate implementation",
            scenario_text="task routing",
            prompt_hint=(
                "Task routing: handle social/simple work directly, delegate implementation: "
                "Handle chat directly and delegate concrete implementation work."
            ),
            visibility_mode=AFFORDANCE_VISIBILITY_RESIDENT,
        )
        class DeclaredResidentProvider:
            module_id = "declared_resident_plugin"

        handle = _FakeHandle(module_id="declared_resident_plugin", introspection_provider=DeclaredResidentProvider())
        self.service.register_declared_module(handle)

        prompt = core.build_canonical_prompt(PromptAssemblyContext())
        system = prompt.messages[0]["content"]
        reminder = str(prompt.messages[-1]["content"])
        guidance = reminder.split("<behavior_guidance>", 1)[1].split("</behavior_guidance>", 1)[0]

        self.assertNotIn("\n<behavior_guidance>\n", system)
        self.assertEqual(guidance.count("Behavior guidance is behavior-owned routing metadata"), 1)
        self.assertEqual(guidance.count("Consider matching guidance before choosing a route"), 1)
        self.assertIn("- OLED expression: Use the OLED expression capability sparingly.", guidance)
        self.assertIn(
            "- Task routing: handle social/simple work directly, delegate implementation: "
            "Handle chat directly and delegate concrete implementation work.",
            guidance,
        )
        self.assertNotIn("OLED expression: OLED expression:", guidance)
        self.assertNotIn(
            "Task routing: handle social/simple work directly, delegate implementation: "
            "Task routing: handle social/simple work directly, delegate implementation:",
            guidance,
        )

    def test_module_capabilities_are_auto_declared_as_affordances(self) -> None:
        subtree = MountedSubtreeHandle(
            module_id="demo",
            descriptors=[
                CapabilityDescriptor(
                    name="demo_show",
                    canonical_path="demo_show",
                    family="introspection",
                    description="Show demo module state",
                    source="builtin:demo",
                    display_name="demo show",
                    aliases=("demo_show",),
                    module_id="demo",
                ),
                CapabilityDescriptor(
                    name="demo_run",
                    canonical_path="demo_run",
                    family="demo",
                    description="Run demo operation",
                    source="builtin:demo",
                    display_name="demo run",
                    aliases=("demo_run",),
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

        self.assertIsNone(self.repository.get_affordance("declared.capability.demo.demo_run"))
        self.assertIn("demo_show", refs)
        self.assertIn("demo_run", refs)

from __future__ import annotations
from pal.bunshin.runner_components.models import BunshinRuntimeBundle
from pal.bunshin.runner_components.models import BunshinAgentLoopState
from pal.bunshin.runner_components.runtime_build import _bunshin_noop_failure_handler
from pal.bunshin.runner_components.llm_settings import _bunshin_llm_request_metadata
from pal.bunshin.runner_components.llm_settings import _bunshin_temperature
from pal.bunshin.runner_components.prompt_values import _llm_tools_for_allowed
from dataclasses import dataclass, replace
from pal.core import AgentTurnRuntime, MainContext
from pal.core.prompt_fragment_registry import PromptFragmentRegistry
from pal.core.runtime_config import RuntimeConfig
from pal.core.turns import TurnContinuation
from pal.llm.ir import LLMRequestIR
from pal.memory.prompt import MemoryPromptFragmentProvider
from pal.skill.prompt import SkillPromptFragmentProvider
from pal.bunshin.compact import BunshinCompactionPolicy
from pal.bunshin.prompt_adapter import BunshinPromptFragmentProvider
from pal.shared import BunshinInvocationPack
from pal.bunshin.runner_components.prompt_context import PromptContext
from pal.bunshin.runner_components.reporter import Reporter
from pal.bunshin.runner_components.tool_observation import ToolObservation
from pal.bunshin.runner_components.adapters import _BunshinLLMRuntimeAdapter, _BunshinOutputPort


@dataclass
class AgentRuntime:
    prompt_context: PromptContext
    reporter: Reporter
    tool_observation: ToolObservation
    pack: BunshinInvocationPack
    run_id: str

    def build_bunshin_agent_runtime(
        self,
        bundle: BunshinRuntimeBundle,
        state: BunshinAgentLoopState,
        continuation: TurnContinuation,
    ) -> AgentTurnRuntime:
        llm_runtime = _BunshinLLMRuntimeAdapter(self.reporter, self.tool_observation.heartbeat, bundle.llm_runtime, state)
        output_port = _BunshinOutputPort(self.reporter)
        prompt_fragment_registry = self.build_bunshin_prompt_fragment_registry()
        context = MainContext(
            execution_runtime=state.execution_runtime,
            prompt_fragment_registry=prompt_fragment_registry,
            port_registry={
                "llm:llm": llm_runtime,
                "memory:memory": state.memory_service,
                "agent_io:output": output_port,
            },
        )

        def adapt_request(prompt: LLMRequestIR) -> LLMRequestIR:
            return replace(
                prompt,
                policy=replace(
                    prompt.policy,
                    temperature=_bunshin_temperature(self.pack, fallback=prompt.policy.temperature),
                    tool_choice=(
                        "required"
                        if state.pending_output_length_recovery_note
                        else prompt.policy.tool_choice
                    ),
                ),
                metadata={**dict(prompt.metadata), **_bunshin_llm_request_metadata(self.pack, self.run_id)},
            )

        runtime = AgentTurnRuntime.build(
            context=context,
            config=bundle.config or RuntimeConfig.defaults(),
            debug_log_prompt=lambda _continuation, request: self.reporter.debug_log_bunshin_llm_request(state, request),
            debug_log_outcome=lambda _continuation, outcome: self.reporter.debug_log_bunshin_llm_outcome(state, outcome),
            debug_log_reply=lambda _continuation, text: self.reporter.debug_log_bunshin_reply(text),
            build_llm_tool_contracts=lambda: _llm_tools_for_allowed(
                state.execution_runtime,
                self.pack.allowed_capabilities,
                action_only=bool(state.pending_output_length_recovery_note),
            ),
            handle_failure_async=_bunshin_noop_failure_handler,
            render_failure_feedback_text=lambda feedback: str(feedback or ""),
            should_enter_failure_flow_for_tool_result=lambda _tool_result: False,
            handle_llm_provider_errors=False,
            request_adapter=adapt_request,
            execute_tool_async=lambda call, **kwargs: self.tool_observation.execute_bunshin_tool_with_observation(
                state,
                continuation,
                call,
                **kwargs,
            ),
            compaction_policy=BunshinCompactionPolicy(),
            compaction_clock_provider=lambda: state.llm_round_count,
        )
        return runtime

    def build_bunshin_prompt_fragment_registry(self) -> PromptFragmentRegistry:
        prompt_fragment_registry = PromptFragmentRegistry()
        prompt_fragment_registry.register(
            BunshinPromptFragmentProvider(
                scaffold_factory=self.prompt_context.prompt_scaffold,
                role_context_factory=self.prompt_context.render_durable_role_context,
            )
        )
        prompt_fragment_registry.register(
            MemoryPromptFragmentProvider(
                include_l1_recent_context=True,
            )
        )
        prompt_fragment_registry.register(SkillPromptFragmentProvider())
        return prompt_fragment_registry

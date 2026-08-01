from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pal.core.prompt_compiler import PromptCompiler
from pal.core.compaction import CompactionEngine, CompactionPolicy
from pal.core.runtime_config import RuntimeConfig
from pal.core.tool_stagnation import ToolStagnationGuardProcess
from pal.core.turn_executor import TurnExecutor
from pal.llm.ir import LLMRequestIR
from pal.shared import PromptAssemblyContext


@dataclass
class AgentTurnRuntimeState:
    """The executor-owned state shared by every agent host."""

    diagnostics: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class AgentTurnGuardHost:
    """Minimal guard owner for hosts that do not need Pal's TurnManager."""

    guard: ToolStagnationGuardProcess


@dataclass
class AgentTurnRuntime:
    """Shared prompt/execution kernel used by Pal and Minion.

    Agent hosts supply ports, policy callbacks, and optional request decoration.
    The mechanics of prompt compilation and effect execution stay identical.
    """

    context: Any
    config: RuntimeConfig
    prompt_compiler: PromptCompiler
    executor: TurnExecutor
    compaction_engine: CompactionEngine
    state: Any
    guard_host: Any

    @classmethod
    def build(
        cls,
        *,
        context: Any,
        config: RuntimeConfig,
        call_port_async: Callable[..., Awaitable[Any]],
        debug_log_prompt: Callable[..., None],
        debug_log_outcome: Callable[..., None],
        debug_log_reply: Callable[..., None],
        build_llm_tool_contracts: Callable[[], list[dict[str, object]]],
        handle_failure_async: Callable[..., Awaitable[Any]],
        render_failure_feedback_text: Callable[[Any], str],
        should_enter_failure_flow_for_tool_result: Callable[[Any], bool],
        state: Any | None = None,
        guard_host: Any | None = None,
        request_adapter: Callable[[LLMRequestIR], LLMRequestIR] | None = None,
        execute_tool_async: Callable[..., Awaitable[Any]] | None = None,
        handle_llm_provider_errors: bool = True,
        compaction_policy: CompactionPolicy | None = None,
        compaction_clock_provider: Callable[[], int] | None = None,
    ) -> "AgentTurnRuntime":
        resolved_state = state if state is not None else AgentTurnRuntimeState()
        resolved_guard_host = guard_host
        if resolved_guard_host is None:
            resolved_guard_host = AgentTurnGuardHost(
                guard=ToolStagnationGuardProcess.from_config(config),
            )
        prompt_compiler = PromptCompiler(context)
        if compaction_policy is None:
            from pal.core.pal_compaction import PalCompactionPolicy

            compaction_policy = PalCompactionPolicy()
        compaction_engine = CompactionEngine(
            policy=compaction_policy,
            max_attempts=max(
                1,
                int(
                    getattr(
                        config,
                        "llm_compaction_retry_attempts",
                        3,
                    )
                    or 1
                ),
            ),
            timeout_seconds=float(
                getattr(config, "llm_compaction_timeout_seconds", 180.0)
                or 180.0
            ),
        )

        def build_canonical_prompt(
            assembly_context: PromptAssemblyContext,
            *,
            max_output_tokens: int = 1024,
            model_hint: str | None = None,
        ) -> LLMRequestIR:
            request = prompt_compiler.build_canonical_prompt(
                assembly_context,
                max_output_tokens=max_output_tokens,
                model_hint=model_hint,
            )
            return request_adapter(request) if request_adapter is not None else request

        executor = TurnExecutor(
            context,
            resolved_state,
            resolved_guard_host,
            call_port_async=call_port_async,
            build_canonical_prompt=build_canonical_prompt,
            debug_log_prompt=debug_log_prompt,
            debug_log_outcome=debug_log_outcome,
            debug_log_reply=debug_log_reply,
            build_llm_tool_contracts=build_llm_tool_contracts,
            handle_failure_async=handle_failure_async,
            render_failure_feedback_text=render_failure_feedback_text,
            should_enter_failure_flow_for_tool_result=should_enter_failure_flow_for_tool_result,
            handle_llm_provider_errors=handle_llm_provider_errors,
            execute_tool_async=execute_tool_async,
            config=config,
            compaction_engine=compaction_engine,
            compaction_clock_provider=compaction_clock_provider,
        )
        return cls(
            context=context,
            config=config,
            prompt_compiler=prompt_compiler,
            executor=executor,
            compaction_engine=compaction_engine,
            state=resolved_state,
            guard_host=resolved_guard_host,
        )


__all__ = [
    "AgentTurnGuardHost",
    "AgentTurnRuntime",
    "AgentTurnRuntimeState",
]

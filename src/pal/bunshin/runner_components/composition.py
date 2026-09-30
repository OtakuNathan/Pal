from __future__ import annotations
from pal.bunshin.runner_components.models import EventWriter
from pal.bunshin.runner_components.models import DecisionReader
from dataclasses import dataclass
from pathlib import Path
from pal.shared import BunshinInvocationPack
from pal.bunshin.runner_components.artifacts import Artifacts
from pal.bunshin.runner_components.memory_results import MemoryResults
from pal.bunshin.runner_components.reporter import Reporter
from pal.bunshin.runner_components.research_budget import ResearchBudget
from pal.bunshin.runner_components.session_memory import SessionMemory
from pal.bunshin.runner_components.status import Status
from pal.bunshin.runner_components.tool_session import ToolSession
from pal.bunshin.runner_components.completion import Completion
from pal.bunshin.runner_components.control import Control
from pal.bunshin.runner_components.heartbeat import Heartbeat
from pal.bunshin.runner_components.prompt_context import PromptContext
from pal.bunshin.runner_components.results import Results
from pal.bunshin.runner_components.review_evidence import ReviewEvidence
from pal.bunshin.runner_components.session_checkpoints import SessionCheckpoints
from pal.bunshin.runner_components.text_deliverables import TextDeliverables
from pal.bunshin.runner_components.tool_execution import ToolExecution
from pal.bunshin.runner_components.llm_rounds import LlmRounds
from pal.bunshin.runner_components.tool_observation import ToolObservation
from pal.bunshin.runner_components.agent_runtime import AgentRuntime
from pal.bunshin.runner_components.agent_session import AgentSession
from pal.bunshin.runner_components.invocation import Invocation


@dataclass(frozen=True)
class RunnerComponents:
    artifacts: Artifacts
    memory_results: MemoryResults
    reporter: Reporter
    research_budget: ResearchBudget
    session_memory: SessionMemory
    status: Status
    tool_session: ToolSession
    completion: Completion
    control: Control
    heartbeat: Heartbeat
    prompt_context: PromptContext
    results: Results
    review_evidence: ReviewEvidence
    session_checkpoints: SessionCheckpoints
    text_deliverables: TextDeliverables
    tool_execution: ToolExecution
    llm_rounds: LlmRounds
    tool_observation: ToolObservation
    agent_runtime: AgentRuntime
    agent_session: AgentSession
    invocation: Invocation


def build_runner_components(
    *, runtime_root: Path, pack: BunshinInvocationPack, bunshin_id: str, run_id: str, write_event: EventWriter,
    read_decision: DecisionReader,
) -> RunnerComponents:
    artifacts = Artifacts(pack=pack)
    memory_results = MemoryResults(run_id=run_id)
    reporter = Reporter(bunshin_id=bunshin_id, pack=pack, run_id=run_id, write_event=write_event)
    research_budget = ResearchBudget(pack=pack)
    session_memory = SessionMemory()
    status = Status()
    tool_session = ToolSession()
    completion = Completion(artifacts=artifacts, pack=pack, runtime_root=runtime_root)
    control = Control(reporter=reporter, bunshin_id=bunshin_id, pack=pack, read_decision=read_decision, run_id=run_id)
    heartbeat = Heartbeat(reporter=reporter, pack=pack)
    prompt_context = PromptContext(tool_session=tool_session, pack=pack, runtime_root=runtime_root)
    results = Results(artifacts=artifacts, memory_results=memory_results, status=status)
    review_evidence = ReviewEvidence(reporter=reporter)
    session_checkpoints = SessionCheckpoints(
        artifacts=artifacts, completion=completion, memory_results=memory_results, research_budget=research_budget,
        review_evidence=review_evidence, tool_session=tool_session, pack=pack, runtime_root=runtime_root,
    )
    text_deliverables = TextDeliverables(artifacts=artifacts, prompt_context=prompt_context, bunshin_id=bunshin_id, pack=pack, run_id=run_id)
    tool_execution = ToolExecution(
        control=control, research_budget=research_budget, review_evidence=review_evidence, status=status,
        tool_session=tool_session, pack=pack, run_id=run_id,
    )
    llm_rounds = LlmRounds(
        completion=completion, control=control, heartbeat=heartbeat, prompt_context=prompt_context, reporter=reporter,
        status=status, text_deliverables=text_deliverables, tool_session=tool_session, pack=pack,
    )
    tool_observation = ToolObservation(heartbeat=heartbeat, reporter=reporter, tool_execution=tool_execution, tool_session=tool_session)
    agent_runtime = AgentRuntime(prompt_context=prompt_context, reporter=reporter, tool_observation=tool_observation, pack=pack, run_id=run_id)
    agent_session = AgentSession(
        agent_runtime=agent_runtime, artifacts=artifacts, control=control, llm_rounds=llm_rounds,
        memory_results=memory_results, reporter=reporter, review_evidence=review_evidence,
        session_checkpoints=session_checkpoints, session_memory=session_memory, status=status,
        tool_session=tool_session, bunshin_id=bunshin_id, pack=pack, run_id=run_id, runtime_root=runtime_root,
    )
    invocation = Invocation(
        agent_session=agent_session, artifacts=artifacts, completion=completion, control=control, reporter=reporter,
        results=results, status=status, text_deliverables=text_deliverables,
    )
    return RunnerComponents(
        artifacts=artifacts,
        memory_results=memory_results,
        reporter=reporter,
        research_budget=research_budget,
        session_memory=session_memory,
        status=status,
        tool_session=tool_session,
        completion=completion,
        control=control,
        heartbeat=heartbeat,
        prompt_context=prompt_context,
        results=results,
        review_evidence=review_evidence,
        session_checkpoints=session_checkpoints,
        text_deliverables=text_deliverables,
        tool_execution=tool_execution,
        llm_rounds=llm_rounds,
        tool_observation=tool_observation,
        agent_runtime=agent_runtime,
        agent_session=agent_session,
        invocation=invocation,
    )

"""Three-party compact composition used by behavioral acceptance tests."""
from dataclasses import replace
from pal.llm.compaction_engine import CompactionRunResult
from pal.foundation.diagnostics import exception_report

async def execute_compaction(engine, snapshot, *, llm_runtime, memory_service, after_commit=None,
                             replay_guard=None, commit_guard=None, commit_hooks=None):
    result = await engine.run(snapshot, llm_runtime=llm_runtime, replay_guard=replay_guard,
                              commit_hooks=commit_hooks)
    if result.status != "candidate":
        return result
    installed = await memory_service.install_compaction(snapshot, summary_entry=result.summary_entry,
        after_commit=after_commit, commit_guard=commit_guard, commit_hooks=commit_hooks)
    if isinstance(installed, Exception):
        return replace(result, status="commit_failed", failures=(*result.failures, exception_report(installed)))
    return replace(result, status="compacted", memory_result=installed)

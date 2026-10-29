# Second Tool Efficiency Sampling: Petra Restart Troubleshooting

> Historical record, archived on 2026-10-05 from `docs/tool_efficiency_sample_20260923_petra.md`.
> Statements about implementation, deployment and test results describe the
> original work, not the current checkout or running host. See the
> [current documentation index](../../README.md).

Source: `/tmp/pal/pal.log`, mtime 2026-09-23 17:44 BST. This file is a snapshot of context and replies from the last request, not a per-request timing trace; the following can only count visible history and cannot infer precise LLM latency, cost, or complete rounds before compaction.

## Key Findings

The sample still used the old checklist guidance, tool results containing full echo output, and empty search results without filter_suggestions. The source code changes from this round are not reflected in these calls, so this cannot be treated as a post-optimization comparison sample.

The restart task starts with the user's "restart it" at message index 31 and ends with the reply at index 74. In between there are 20 assistant messages with tool_call, each with exactly one tool call, plus a final text reply. This is not the full provider attempt count; retries cannot be recovered from this snapshot.

| Category | Tool-call rounds | Observations |
| --- | ---: | --- |
| Behavior/manual path | 7 | advise_behavior, 3x search_tools, read_tool, skill_search, skill_inject |
| Checklist management | 5 | upsert, 3x check, clear; each occupying its own round |
| Health tool discovery | 2 | family=endpoint caused empty results; the second, relaxed search found the target |
| Remote execution and recheck | 4 | inspect service, restart, wait-and-recheck, native sampling |
| Channel queries | 2 | list endpoints, then query bridge health for the known petra |

## Specific Sources of Waste

1. **Discovery filter categories diverge from everyday intuition.** skill_search is in the operation namespace, yet the model first used inspect/introspection; only after two wrong filter attempts did it switch to action and find it. The existing descriptions of search_tools interpret inspect as checking status and action as doing things, which does not accurately predict owner-registered categories. The health check also again misused family=endpoint. The already-implemented zero-result recovery suggestions can mitigate this, but more direct guidance is to omit namespace/family for unknown categories and not guess the namespace from "read-only".
2. **Known names still went through the full search chain.** At the start of discovery, the query already explicitly contained pal.self.maintenance; after finding skill_inject and knowing the name parameter, it still did read_tool(skill_search) → skill_search → skill_inject. Consulting the operational constraints itself was justified; the problem is the extra steps before reaching the manual. skill_guide only gives the search→inject path for unfamiliar scenarios and lacks an equally explicit direct path for known names.
3. **advise_behavior returned no relevant suggestions.** The input already specified target, launchd, permissions, and the verification plan, yet the return was Minion memory absorption, file reads, edit_file, and file_state. This shows the routing match quality was insufficient this time; one cannot conclude from this that all behaviors are useless, nor treat it as a valuable pre-check. The historical "must ask whether to absorb memories" rule in the sample is unrelated to the current task and the rule's scope of application should be narrowed.
4. **Checklist maintenance consumed rounds on its own.** The preceding investigation even upserted three pending items only after evidence gathering, then checked three times in a row and cleared once. For end-of-work bookkeeping, this added no investigation information. The new automatic clear can eliminate the final round; other updates should still share a round with actual work, or be omitted on simple tasks that do not need a checklist.
5. **Result volume came from the commands themselves.** The three shell results in the investigation segment were about 15.5K, 8.7K, and 11.5K characters, containing repeated native stacks and broad SQLite whole-repository searches; no specific Python caller/database attribution was obtained. Pagination is not a substitute for scope planning. Sampling and code search scope should be chosen around "can the next piece of evidence distinguish hypotheses", rather than adding more silent truncation.

## Effective Behaviors

- run_shell used the configured target=3 and did not detour to hand-written SSH.
- Multiple-range reads of the same local file used read_file(ranges=...), with three files called in the same round.
- The restart was genuinely performed only once; the PID change was verified, and bridge healthy was not claimed as main-runtime recovery.
- After the restart, native sampling verified that the same busy point recurred, which has diagnostic value.
- When the user later said "it's stuck again", the process check and sampling were combined into a single run_shell, using only one tool round. This shows the tools themselves do not require the earlier verbose path.

## Next Trade-offs

Prioritize activating the completed tool fixes, then do comparable sampling. Further tightening of namespace/family descriptions, direct-path guidance for known skill names, and irrelevant candidates for behavior is possible; no need to add another general-purpose orchestration framework. For tasks like restarts that already have a target and authorization, the process can be planned as a small number of steps with real dependencies, but a fixed round count is not guaranteed, nor are necessary first-time operational constraint checks skipped.

This analysis used only local records; there was no connection to Petra, no process restart, and no checking or modification of its database. The SQLite native stack is still insufficient to prove database corruption.

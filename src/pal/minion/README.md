# minion

Owns:
- durable workflow, aggregate, role-session, and worker-process orchestration
- role-specific prompt/profile policy
- Minion task-continuity compaction policy
- fresh runtime schema cutover and archive policy

Does not own:
- a second prompt compiler, turn executor, memory recall implementation, or
  tool facade
- channel transport
- capability canonical-path exposure to an LLM

Interaction rule:
- Pal and Minion use the shared `core.AgentTurnRuntime`; Minion supplies only
  host ports, prompt fragments, tool observation hooks, and compaction policy
- one logical role session owns file snapshots and pager handles; they expire
  at semantic input `N+5` and become inaccessible when that role session exits
- the append-only `protocol.jsonl` is the full tool journal; the checkpoint
  stores only its bounded prompt projection plus a journal cursor
- checkpoint schema v4 stores request settings, registry generation hash, and
  the L2 hot projection needed for process recovery
- Minion runtime schema v25 is a fresh cutover. Older or unrecognized runtime
  databases are archived atomically; only explicit profile and family
  overrides are copied after validation
- legacy workflow state, aliases, managed seed migrations, and old checkpoint
  schemas are not migrated in place

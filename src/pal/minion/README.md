# minion

Owns:
- durable workflow, aggregate, role-session, and worker-process orchestration
- role-specific prompt/profile policy
- Minion v3 semantic work-checkpoint policy
- fresh runtime schema cutover and archive policy

Does not own:
- a second prompt compiler, turn executor, memory recall implementation, or
  tool facade
- channel transport
- capability canonical-path exposure to an LLM

Interaction rule:
- Pal and Minion use the shared `core.AgentTurnRuntime`; Minion supplies only
  host ports, prompt fragments, tool observation hooks, and compaction policy
- `pal.compaction.minion.v3` records only `technical_route`, `active_work`,
  `active_errors`, `active_issues`, and `next_actions`; it emits neither
  private chain-of-thought nor durable-memory candidates
- Architect and Architecture Reviewer keep complete `task.yaml` authority.
  Coder and Module Verifier keep their bound work view, contracts, and
  checklist authority. These anchors are mechanically projected and are never
  rewritten by compaction
- the Minion compaction clock advances only for successful consumable LLM
  rounds. Provider errors, truncation, `compact_required`, tools, and
  compaction calls do not advance it
- one logical role session owns file snapshots and pager handles; they expire
  at semantic input `N+5` and become inaccessible when that role session exits
- checkpoint schema v5 serializes the complete current L1 working set, the L2
  hot cache, request settings, clocks, and registry generation hash. It does
  not maintain a second tool-protocol journal or compact-summary truth source
- closed tool calls and results enter L1 incrementally. Compaction reads only
  that frozen L1; role contracts, checklists, task fallback, and memory recall
  remain independently projected authority
- compaction retires the active provider prompt projection. It does not advance
  the independent semantic-input pager clock or expire an `N+5` pager handle
- Minion runtime schema v25 is a fresh cutover. Older or unrecognized runtime
  databases are archived atomically; only explicit profile and family
  overrides are copied after validation
- legacy workflow state, aliases, managed seed migrations, and old checkpoint
  schemas are not migrated in place

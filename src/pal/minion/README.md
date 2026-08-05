# minion

Owns:
- durable workflow, aggregate, role-session, and worker-process orchestration
- Manager-compiled architecture templates, Family specializations, and
  role-TOML playbooks
- immutable `GraphIR` compilation, module views, and WorkItem ledgers
- producer/checker cycles for planning and for every executable graph node
- graph readiness, reverse finding propagation, replan reuse, and publication
  of the one authored terminal sink
- Minion v3 semantic work-checkpoint policy
- fresh runtime schema cutover and archive policy

Does not own:
- a second prompt compiler, turn executor, memory recall implementation, or
  tool facade
- channel transport
- capability canonical-path exposure to an LLM

Interaction rule:
- Architect authors one Family-specialized `architect.yaml`. `GraphCompiler`
  validates and compiles it to immutable `GraphIR` plus a source map; it never
  invents an integration, assembly, scenario, or system-verification node
- each Family pins a `graph_satellite.j2` alongside its schema and authoring
  template. That projection explains the Family's authored semantics to its
  execution domain and emits opaque per-node satellite data. Shared graph code
  only validates topology, persists/hash-compares satellites, and carries the
  common workspace policy; it does not interpret Family requirement, scenario,
  contract, or domain fields
- `WorkflowCoordinator` is the single mechanical owner of `PlanCycle` and
  `GraphExecution`. `GraphIR` is data; `GraphExecution` alone decides
  readiness, traversal, repair routing, replan reuse, and sink publication
- planning is Architect producer -> Architecture Reviewer checker -> Human.
  Every executable graph node, including the authored sink, is a Coder
  producer -> Verifier checker cycle. Software Coders run in parallel from
  accepted contracts; the sink Coder owns the source-level entrypoint and
  composition, while Manager waits for accepted dependency Candidates before
  assembling the sink checker worktree. The sink Verifier owns end-to-end,
  system, and delivery verification
- one module identity owns one reusable worktree and its Coder/Verifier logical
  sessions. The roles share the same Git baseline but have distinct writable
  developer/verifier test directories. Replan preserves the worktree and both
  sessions while name and responsibility stay stable; acceptance is carried
  only when the contract and incoming edges are also unchanged
- logical role sessions are durable data, not resident processes. A
  `RoleProcessShell` materializes an OS process only after acquiring capacity;
  capacity is released only after the process group is reaped, registration and
  heartbeats close, and worktree occupancy is released. Sleeping or queued
  logical sessions consume no capacity
- Pal and Minion use the shared `core.AgentTurnRuntime`; Minion supplies only
  host ports, prompt fragments, tool observation hooks, and compaction policy
- `pal.compaction.minion.v3` records only `technical_route`, `active_work`,
  `active_errors`, `active_issues`, and `next_actions`; it emits neither
  private chain-of-thought nor durable-memory candidates
- Architect and Architecture Reviewer keep complete `task.yaml` authority.
  Manager renders one Family-specialized `architect.yaml`; Architect fills that
  form and submits it through the Manager's pinned schema validator. Reviewer
  submits structured findings. Coder and Module Verifier keep only their bound
  module view, local contract, code, and checklist authority
- the authored sink Coder still receives only its local module view. Manager
  projects whole-graph requirements, scenarios, and entrypoints into a separate
  `SystemDeliveryViewArtifact` visible only to the sink Verifier
- Minion Git reads are a Manager-scoped review surface, not repository
  archaeology: commands are limited to the bound Candidate range, owned module
  paths, local tests, and dependency public contract paths
- role profile fragments are playbooks, Manager-derived views are inputs, and
  the WorkItem checklist is the execution driver; none is a second contract
- every family uses the same Architect/Reviewer/Implementation/Verifier graph.
  Architect and Reviewer require profile participants; Implementation and
  Verifier are either both profile-backed or both explicit `null` participants
- role profile and execution harness are separate contracts. Each attempt
  captures one immutable harness generation; attached role-specialized
  harnesses take priority and Pal remains the universal fallback
- the Minion compaction clock advances only for successful consumable LLM
  rounds. Provider errors, truncation, `compact_required`, tools, and
  compaction calls do not advance it
- one logical role session owns file snapshots and pager handles; they expire
  at semantic input `N+5` and become inaccessible when that role session exits
- checkpoint schema v8 stores a non-sensitive routing/fencing header plus one
  authenticated encrypted logical-process payload. Module-owned state is
  serialized behind that envelope; Manager routes it opaquely and only the
  target worker decrypts it. Older plaintext checkpoint schemas are rejected
- the encrypted coroutine payload also owns assignment-local artifact/evidence
  lists, memory candidates, and resource-budget counters. Native-process
  restart restores them only when the Manager input key is unchanged; a new
  semantic assignment mechanically starts those invocation-local fields empty
- checkpoints atomically replace the latest encrypted payload at every closed
  tool-protocol safe point, including role-stage entry, structured terminal
  boundaries, and cooperative restart. A hard crash therefore resumes from
  the latest closed boundary plus durable worktree, checklist, findings, and
  pending Manager input; no in-flight tool batch is checkpointed
- closed tool calls and results enter L1 incrementally. Compaction reads only
  that frozen L1; role contracts, checklists, task fallback, and memory recall
  remain independently projected authority
- Minion uses the selected endpoint/model output limit by default. If a response
  reaches that limit, provider-committed tool items continue normally; otherwise
  the harness discards the uncommitted response and forces one bounded file,
  checklist, finding, question, or submit action instead of generic continuation
- compaction retires the active provider prompt projection. It does not advance
  the independent semantic-input pager clock or expire an `N+5` pager handle
- Minion runtime schema v29 is a fresh cutover. Older or unrecognized runtime
  databases are archived atomically; only explicit profile and family
  overrides are copied after validation
- legacy workflow state, aliases, managed seed migrations, and plaintext
  checkpoint schemas through v7 are not migrated in place
- Manager records one origin/current delivery binding on the Task, outside
  `task.yaml`, `GraphIR`, workflow payloads, and worker context. Ordinary input
  from another channel never changes it; `minion_rebind_task_delivery` changes
  only that reply target. A dead target falls back to a connected recovery
  socket, otherwise the durable notification remains pending

# core

Owns:
- event main loop
- turn coordination
- turn computation suspend/resume
- minimal runtime-owned state
- subsystem orchestration
- module lifecycle governance
- registry ownership for visibility and mount policy
- prompt fragment registry and prompt assembly seam
- the shared `AgentTurnRuntime` prompt/turn-execution kernel used by Pal and
  Minion hosts
- shared compaction orchestration: immutable snapshots, atomic history units,
  budget preflight, an independently configured model-attempt budget,
  validation, degraded checkpoints, and commit ordering
- stagnation guard orchestration

Does not own:
- durable truth
- direct database access
- side effects
- plugin implementations
- capability invocation mechanics

Exposes:
- `PalCore`
- `TurnManager`
- `MainLoop`
- `EventDispatcher`
- event and handler registries
- `PromptFragmentRegistry`
- `AgentTurnRuntime`
- `CompactionEngine`, `CompactionPolicy`, `CompactionSnapshot`, and
  `CompactionRunResult`
- mailbox and turn-effect contracts

Interaction rule:
- `PalCore` is the only governance center
- Pal and Minion supply host policy and ports to the same `AgentTurnRuntime`;
  they do not maintain separate prompt compilers or turn executors
- automatic compaction is requested only by the real model context-budget
  path; host clocks annotate snapshots but never trigger fixed-round compact
- the immutable L1 snapshot is compaction's sole semantic input. Current input
  and each closed tool batch are committed to L1 before compaction; provider
  projections, recall caches, and role anchors are not parallel truth sources
- exact provider tool-continuation fields belong to the active logical turn.
  They survive an in-flight checkpoint, but turn closure releases them; L1
  retains only the provider-neutral tool semantics used by compaction and
  future context
- one logical turn may create at most three compact generations. Semantic
  generation gets three attempts before a mechanical checkpoint, and the
  prompt plus local validator cap visible checkpoint JSON at an estimated
  20,000 tokens while preserving provider-declared model reasoning headroom
- a policy owns schema, prompt, validation, rendering, degraded semantics, and
  whether durable-memory candidates are allowed
- `Execution` is the only official invocation plane
- `Pal` should call capabilities through `PalCore -> Execution`
- turn computations yield effect requests and are resumed by `PalCore`
- `MainLoop` drains mailbox-backed sources after async wakeups rather than
  pulling module-private queues on an idle timer
- modules may register ports, event sources, providers, and introspection
  surfaces with `PalCore`
- `register_with_core(...)` is also the hydration seam for Capability Forest
  subtree generation
- `PalCore` governs subtree mount/detach/reattach, but `Execution` physically
  stores and dispatches those subtrees
- modules must not directly call other modules' runtime, service, or repository
  implementations
- `PalCore` does not supervise OS processes; that belongs to `supervisor`

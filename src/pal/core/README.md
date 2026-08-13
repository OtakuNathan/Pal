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
  validation, transactional commit ordering, and no mutation on failure
- stagnation guard orchestration
- module runtime-state orchestration: deterministic snapshot/restore order,
  reverse-order whole-runtime reset, exact incarnation checks, and encrypted
  composite envelopes

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
- all channel input enters one resident mailbox and one L1/execution lifetime;
  channels select only the response transport and never create a persona,
  conversation, or execution scope
- Pal and Minion supply host policy and ports to the same `AgentTurnRuntime`;
  they do not maintain separate prompt compilers or turn executors
- automatic compaction is requested only by the real model context-budget
  path; host clocks annotate snapshots but never trigger fixed-round compact
- the immutable L1 snapshot is compaction's sole semantic input. Current input
  and each closed tool batch are committed to L1 before compaction; provider
  projections, recall caches, and role anchors are not parallel truth sources
- exact provider tool-continuation fields belong to the active logical turn.
  Snapshots occur only at closed protocol boundaries; crash restore removes
  any unmatched/duplicate protocol and incomplete stream fragments instead of
  fabricating results. Turn closure releases provider-only continuation data
- one logical turn may create at most three compact generations. Semantic
  generation gets three attempts before that compact RPC fails with memory
  unchanged. The prompt plus local validator cap visible checkpoint JSON at
  half the selected input budget or the absolute 20,000-token ceiling,
  whichever is smaller, while preserving provider-declared model reasoning
  headroom
- a policy owns schema, prompt, validation, rendering, and whether
  durable-memory candidates are allowed
- `Execution` is the only official invocation plane
- `Pal` should call capabilities through `PalCore -> Execution`
- turn computations yield effect requests and are resumed by `PalCore`
- `MainLoop` drains mailbox-backed sources after async wakeups rather than
  pulling module-private queues on an idle timer
- resident reset interrupts the active turn, preserves already queued channel
  messages, and resets module-owned runtime state through their registered
  runtime-state ports before the same mailbox resumes
- modules may register ports, event sources, providers, and introspection
  surfaces with `PalCore`
- `register_with_core(...)` is also the hydration seam for Capability Forest
  subtree generation
- `PalCore` governs subtree mount/detach/reattach, but `Execution` physically
  stores and dispatches those subtrees
- modules must not directly call other modules' runtime, service, or repository
  implementations
- `PalCore` does not supervise OS processes; that belongs to `supervisor`

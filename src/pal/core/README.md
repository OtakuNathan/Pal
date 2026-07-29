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
- mailbox and turn-effect contracts

Interaction rule:
- `PalCore` is the only governance center
- Pal and Minion supply host policy and ports to the same `AgentTurnRuntime`;
  they do not maintain separate prompt compilers or turn executors
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

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
- stagnation guard orchestration

Does not own:
- durable truth
- direct database access
- side effects
- plugin implementations
- capability invocation mechanics

Exposes:
- `PalCore`
- `TurnRunner`
- `TurnManager`
- `MainLoop`
- `EventDispatcher`
- event and handler registries
- `PromptFragmentRegistry`
- mailbox and turn-effect contracts

Interaction rule:
- `PalCore` is the only governance center
- `Execution` is the only official invocation plane
- `Pal` should call capabilities through `PalCore -> Execution`
- turn computations yield effect requests and are resumed by `PalCore`
- `MainLoop` polls mailbox-backed sources rather than module-private queues
- modules may register ports, event sources, providers, and introspection
  surfaces with `PalCore`
- `register_with_core(...)` is also the hydration seam for Capability Forest
  subtree generation
- `PalCore` governs subtree mount/detach/reattach, but `Execution` physically
  stores and dispatches those subtrees
- modules must not directly call other modules' runtime, service, or repository
  implementations
- `PalCore` does not supervise OS processes; that belongs to `supervisor`

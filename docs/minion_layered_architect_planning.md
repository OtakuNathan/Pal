# Minion Layered Architect Planning

This note records the historical V1 staged-architect experiment. The active V2
contract pipeline is documented in
[`minion_v2_contract_orchestration.md`](minion_v2_contract_orchestration.md) and
does not use ModuleDetailArtifact, milestones, or cursor-driven execution.

Current implementation status:

- Implemented v1 stages: `PlanSketchArtifact` and `ModuleDetailArtifact`.
- Implemented explicit dispatch depth: `planning_depth = "sketch_only"` or
  `planning_depth = "module_detail"`.
- The manager does not infer size. The caller/user chooses the depth.
- The existing `plan_*` builder is reused behind phase-scoped capability groups.
- The manager creates/binds the draft before the minion starts; the model should
  not call `plan_begin` in the sketch stage.
- The manager compiles staged artifacts back into the existing
  `FinalPlanArtifact` for the existing plan-acceptance/coder pipeline.

## Problem

The current software architect path asks one run to inspect a broad goal,
reference material, module boundaries, contracts, tests, negative cases,
milestones, and a dispatchable `FinalPlanArtifact` in one pass.

That works for small changes, but large reference-driven tasks can turn into a
stress test. The architect has to:

- confirm scope and non-goals
- inspect broad reference roots
- discover functional domains
- design module contracts and dataflow
- produce module-level tests and negative cases
- split coder milestones
- satisfy canonical DAG topology and plan-review schema

The desired architect work is smaller than that. It should first design the
system shape, then fill module details, then let a module planner expand those
details into coder milestones.

## Target Shape

Planning is layered:

1. `requirements_bill` / source gate contract
   - Stable, complete source of truth.
   - Contains source requirements, non-goals, constraints, references, and
     stable requirement ids.
   - Remains fully auditable, but does not have to be fully injected into every
     prompt turn.

2. `PlanSketchArtifact`
   - Produced by the first architect round.
   - Captures global module interaction, topology, contracts, ownership,
     lifecycle, state machines, invariants, and first-layer gates only.
   - Does not contain coder checkpoint milestones.

3. `ModuleDetailArtifact`
   - Produced by subsequent architect rounds, one module at a time.
   - Enriches an accepted sketch module with contract details, tests, negative
     case classes, lifecycle, state, and evidence expectations.

4. `FinalPlanArtifact`
   - Manager-compiled artifact from the submitted sketch plus submitted module
     details.
   - This is the existing dispatchable DAG consumed by plan review, coder, and
     reviewer flow.

The earlier idea of a separate `ContractDAG` / `ImplementationPlan` layer is
not implemented in v1. For now the staged architect compiler emits the existing
`FinalPlanArtifact`.

Planning is intentionally two-layer by default: plan sketch, then module detail.
If a module is so large that it appears to need another sketch layer, the first
round should usually split that module more clearly or ask for human review of
the boundary. Recursive sketching is not part of the default flow.

## Planning Depth

Not every software-engineering work order should use every layer. The caller or
user chooses the lightest planning depth that can preserve the contract; the
manager does not infer task size.

Implemented depths:

1. `sketch_only`
   - Use for small bug fixes, single-module changes, and tasks with obvious
     implementation boundaries.
   - Produce only a compact sketch: scope, touched area/module, relevant
     interface or contract, evidence/reference pointers, acceptance criteria,
     and meaningful negative cases.
   - Coder owns implementation details and may choose the local helper layout.
   - No per-module detail phase.

2. `module_detail`
   - Use for large, reference-heavy, multi-module, lifecycle/stateful, or
     cross-interface work.
   - Produce the global sketch first, then run module-detail fill for each
     module or for the risky modules.
   - Module planner or coder then expands detailed module contracts into
     concrete milestones.

Selection hints:

- Single touched area, low reference pressure, no new public surface:
  `sketch_only`.
- Large reference roots, multiple modules, explicit state machine/lifecycle,
  cross-module interfaces, stubs/mocks, or unclear ownership boundaries:
  `module_detail`.

The point is to avoid running every task through the heaviest architecture path.
Small tasks should not pay the cost of module-detail fan-out, and large tasks
should not be compressed into one full implementation-plan run.

## Round 1: Global Sketch

The first architect round may inspect the broad goal and references, but its
output should stay lightweight.

It should produce:

- confirmed target and non-goals
- feature/domain split
- module list
- module responsibilities
- provided and consumed interfaces
- dependency and dataflow graph
- node-to-node interaction contracts, including data shape, producer/consumer
  direction, lifecycle handoff, ownership transfer or borrowing, and
  invalid-state/error behavior
- ownership, object/resource lifecycle, state-machine, and invariant headlines
- first-layer module and cross-module gates/AC for topology, contracts,
  lifecycle, ownership, state machines, invariants, and requirement coverage
- end-to-end flow gates/AC that traverse the relevant modules and prove
  cross-module handoff
- per-module reference map with key headers, patch files, functions, or hunks
- coarse module-level quality obligations

It should not produce:

- full coder milestones
- checkpoint-admission evidence
- implementation checklists
- module-local positive/negative case detail
- every mapping row or API detail
- a full dispatchable implementation `FinalPlanArtifact`

The sketch role does not need plan-builder lifecycle ceremony. The manager
creates the draft implicitly when the run starts and binds `plan_handle` into
workspace metadata. The current v1 surface is still the existing plan builder,
but phase-scoped:

- `plan_add_module_outline` / `plan_add_module_outlines_batch`
- module interface and constraint/decision/gate-check tools
- `plan_validate_and_submit_for_review`

Sketch module outlines may omit `milestones`. The submitted artifact is
`plan.sketch.json` with type `PlanSketchArtifact`.

## Round 2: Module Detail Fill

After the sketch exists, the manager should checkout one module at a time. The
prompt for a module-detail round should include only:

- the accepted sketch
- the selected module sketch
- requirements covered by that module
- that module's relevant reference paths or reference-map entries
- interfaces connected to that module

The module-detail round should fill:

- ownership details
- lifecycle details
- invariants
- interface shape, lifecycle, ownership, error behavior, and compatibility
- module-level test plan
- positive contract cases
- negative case classes
- high-level milestone skeleton
- evidence expectations
- reference evidence requirements for the future coder/module planner
- explicit mapping from module-local milestones and AC back to sketch
  interfaces, lifecycle rules, ownership rules, state-machine rules,
  invariants, and first-layer gates

By default, module-detail rounds must not change module boundaries. If a detail
round finds that the sketch is wrong, it should emit a `sketch_revision_request`
instead of silently restructuring the global plan.

Like the sketch phase, the module-detail phase does not require explicit begin
tools. The selected module is bound by work-order metadata. In v1 the existing
plan builder is reused:

- `plan_add_milestone_outline`
- acceptance-criteria tools
- module interface repair tools
- `plan_validate_and_submit_for_review`

The runtime injects the bound `plan_handle` and active `module_id` when the tool
schema accepts them. A submitted detail writes
`module_detail.<module_id>.json` with type `ModuleDetailArtifact`.

## Module Planner

The module planner consumes one detailed module contract and expands it into
concrete coder milestones.

Milestones should be interface-centered:

- create or stabilize the module contract surface
- implement provided interfaces or adapters
- prove consumed interfaces with stubs/mocks or consumer probes
- test positive behavior from the contract
- test negative and invalid-state behavior from the module invariants
- prove integration handoff to downstream modules

For small modules, several of these can collapse into one milestone. The goal is
not to invent complex construction ceremony; it is to produce verifiable slices
around module interfaces.

The module planner can keep using implementation-plan builder tooling or a
smaller module-scoped submit surface. It runs after the module contract is
stable, so it should not have to rediscover global topology or full reference
scope.

## Phase-Scoped Tool Surfaces

Tool exposure should be phase-scoped. The runner should not expose the full
architect plan builder to every architect-like role.

Sketch phase tools:

- inspect/search/read tools for target and reference roots
- `architecture_sketch_submit`
- optional `architecture_sketch_apply_delta`
- optional `sketch_revision_request` for self-reported ambiguity

Sketch phase should not expose:

- implementation milestone builders
- acceptance-criteria batch tools
- checkpoint evidence tools
- module-detail submit tools
- coder execution tools

Module-detail phase tools:

- inspect/search/read tools limited to the active module's references
- `module_detail_submit`
- optional `module_detail_apply_delta`
- `sketch_revision_request`

Module-detail phase should not expose:

- global sketch rewrite tools
- topology construction tools
- implementation-plan milestone builders
- coder execution tools

Module-planner phase tools:

- one module's detailed contract
- module-scoped implementation-plan or milestone submit tools
- focused reference read tools for that module

This keeps the model's attention aligned with the phase. The phase itself is the
state machine: starting the run is implicit begin, and successful submit is
implicit close.

## Parallel Module Detail

After a sketch is submitted, module-detail work can fan out concurrently. Each
worker receives one module sketch plus its connected interface summaries,
requirements, and reference-map entries.

Example:

```text
ArchitectureSketch accepted
  -> module_detail(ohos_keyboard)
  -> module_detail(ohos_draw)
  -> module_detail(ohos_font)
  -> module_detail(ohos_image)
  -> module_detail(ohos_window)
fan-in -> FinalPlanArtifact compile/review
```

Concurrency rules:

- a module-detail worker may enrich only its own module
- interface shape changes require `sketch_revision_request`
- module boundaries cannot be silently changed in module-detail work
- manager compiles all submitted details into the existing `FinalPlanArtifact`
- duplicate reference reads should be deduplicated or summarized by the manager

This makes module-level tests, positive cases, negative cases, and evidence
expectations parallelizable instead of forcing one architect run to fill every
module serially.

## Requirement Checkout

The requirements bill remains complete, but prompts should checkout only the
needed slice:

- the sketch round sees the bill index and global scope
- each module-detail round sees requirements mapped to that module
- each module planner sees the detailed module contract and its requirement ids

Coverage should be tracked across layers:

- requirement id -> sketch module
- sketch module -> module detail
- module detail -> implementation milestones
- milestone -> coder evidence/checkpoint

## Reference Handling

Large reference roots should not be treated as prompt body. The first round
should create or refine a reference map. Later rounds should receive only the
reference entries relevant to the active module.

For example, a Qt/OpenHarmony task can start with:

- `OHOS/` headers as the authoritative API surface
- `*.patch` files as read-only behavior evidence

The sketch round maps domains to reference areas. Module-detail and module
planner rounds then inspect only the matching header and patch areas.

## Expected Benefits

- Architect prompt size stays bounded.
- Reference inspection becomes targeted instead of global.
- Module boundaries stabilize before milestone expansion.
- Negative cases remain part of architecture, but at module-contract granularity.
- The canonical implementation plan is generated later from a smaller module
  context.
- Topology/schema repair loops are less likely to consume the whole architect
  run.

## Implementation Notes

Implemented v1:

- Added `PlanSketchArtifact` validation and stage submit.
- Added `ModuleDetailArtifact` validation and stage submit.
- Added phase-scoped capability groups:
  - `minion_plan_builder_sketch`
  - `minion_plan_builder_module_detail`
- Updated built-in `software_engineering.architect` to be sketch-only.
- Added built-in `software_engineering.architect_module_detail`.
- Added manager seeding for stage-bound drafts.
- Added manager compile from sketch/detail artifacts into `FinalPlanArtifact`.
- Added sketch-only compile path.
- Added module-detail fan-out and fan-in path for explicit
  `planning_depth = "module_detail"`.

Not implemented yet:

- Smart depth selection. Depth is explicit caller responsibility.
- A separate `ContractDAG` artifact.
- A separate module planner after module detail.
- First-class `architecture_sketch_submit` / `module_detail_submit` tools. The
  current implementation reuses `plan_validate_and_submit_for_review`.
- Partial/risky-module-only detail selection. Current module-detail depth fans
  out over sketch modules.
- Formal `sketch_revision_request` handling.

The existing plan builder fields are still useful. The change is when they are
filled:

- sketch round fills global module interaction
- module-detail rounds fill ownership, lifecycle, invariants, tests, and
  negative cases per module
- module planner fills concrete coder milestones

# Minion State Machine Inventory and Matrix Baseline

Status: historical V1 inventory. The active V2 implementation is documented in
[`minion_v2_contract_orchestration.md`](minion_v2_contract_orchestration.md).
V1 workflow writes and resume are disabled after the V2 cutover.

This document separates two things that must not be mixed:

1. **Current behavior**: the states, actions, capabilities, and write paths that
   exist in the repository today.
2. **Matrix target**: the state-machine boundaries and transition rules to use
   when the orchestration layer is rebuilt around `next[state][action]`.

The staged architect design itself is documented in
[`minion_layered_architect_planning.md`](minion_layered_architect_planning.md).
This note covers orchestration ownership and handoff, not plan content quality.

## Executive Summary

The current system does not have one work-order state machine. It has several
partially overlapping state projections:

- runner lifecycle in `MinionRunState.status`
- coarse database projection in `minion_work_orders.status`
- profile handoff in `metadata.workflow`
- staged architect progress in `metadata.staged_planning`
- plan governance in `metadata.plan_review`
- coder DAG progress in `metadata.plan_execution.dag_state`
- milestone closure in checkpoints and review gates

Only the runner lifecycle has an explicit transition table. The other state
machines are implemented as conditionals spread across manager, repository,
review orchestration, public capabilities, and control-action handlers.

This has four practical consequences:

1. The same event can update several state fields without one atomic owner.
2. `work_order.status = active` does not prove that anything can make progress.
3. recovery is a collection of special cases rather than replaying a declared
   transition.
4. plan acceptance is coupled to coder dispatch and inherits policy from the
   producer profile, making architect-to-coder handoff fragile.

The next implementation should retain the runner lifecycle as infrastructure
and define four business machines:

1. Architect Producer
2. Plan Governance
3. Plan Execution
4. DAG Node

An architect is an optional producer of a plan candidate. A valid external plan
must be able to enter Plan Governance directly. A coder must consume only an
immutable accepted plan reference.

## Terminology

| Term | Meaning |
| --- | --- |
| task | Long-lived user objective and profile family selection. |
| work order | Durable orchestration aggregate and user-facing progress record. |
| run | One live or terminal minion runner invocation. Multiple runs may belong to one work order. |
| plan candidate | Validatable `FinalPlanArtifact` revision that is not yet accepted. |
| accepted plan | Immutable plan revision plus acceptance marker bound to its SHA-256 and review gate or human override. |
| plan parent | Work order that owns accepted-plan DAG execution. |
| module child | Work order that executes one DAG module, usually as serial milestones. |
| checkpoint | A claimed implementation milestone result awaiting or recording review closure. |
| review gate | Immutable reviewer verdict bound to a plan or checkpoint target. |

## Current State Storage

The following table is the most important inventory. It defines what each
current field actually means and where it is written.

| State surface | Current values | Intended authority today | Main writers |
| --- | --- | --- | --- |
| `MinionRunState.status` | `starting`, `running`, `approval_pending`, `clarification_pending`, `completed`, `failed`, `blocked`, `killed` | Live runner/process lifecycle | `lifecycle.py`, manager, step executor |
| `minion_work_orders.status` | Observed `active`, `running`, `approval_pending`, `blocked`, `completed`, `failed`, `killed`, `archived`; schema accepts arbitrary text. Execution `paused` is currently stored under `plan_execution`, while the work-order projection remains `active`. | Coarse search/UI projection | manager, repository, review orchestrator, capabilities |
| `metadata.workflow.status` | `running`, `detail_planning`, `module_detail_running`, `final_compiling`, `post_gate_pending`, `executing`, `completed`, `blocked`, plus propagated terminal statuses | Cross-profile workflow cursor | manager and control capabilities |
| `metadata.workflow.steps[*].status` | `running`, `completed`, `accepted`, `post_gate_pending`, `blocked`, plus propagated terminal statuses | Per-profile handoff history | `workflow.py`, manager, capabilities |
| `metadata.staged_planning.status` | absent during initial sketch run, then `sketch_completed`, `module_detail_running`, `module_detail_completed`, `final_compiling`, `final_plan_compiled`, `blocked` | Architect sketch/detail/compile progress | manager |
| `staged_planning.detail_modules[*].status` | `pending`, `waiting_for_slot`, `running`, `completed`; failure is often projected to parent `blocked` | One module-detail child cursor | manager detail scheduler |
| `metadata.plan_review.status` | `reviewing`, `acceptance_pending`, `revision_required`, `revision_spawned`, `revision_in_progress`, `revision_blocked`, `human_decision_required`, `edit_requested`, `accepted`, `rejected`, `gate_missing`, `reconcile_failed`, `failed` | Plan review, revision, and human decision | review orchestrator, repository pack builders, manager, capabilities |
| `metadata.plan_execution.status` | `active`, `awaiting_continue`, `running_module`, `paused`, `blocked`, `completed` | Parent coder DAG progress | repository and manager |
| `plan_execution.dag_state.node_status` | `ready`, `blocked`, `running`, `completed`, `failed`, `paused`, `needs_repair`, `stale` | Per-module dependency/execution state | `dag_advancer.py` through repository |
| checkpoint status | `claimed`, `completed` are the durable progression values | Milestone submission and closure | runner/checkpoint tools and review-gate store |
| review gate verdict | `pass`, `fail`, `partial` | Immutable semantic review decision | reviewer capability and review-gate store |

`minion_work_orders.status` is currently a lossy projection. For example,
`active` can mean an architect is running, module detail is waiting for a slot,
a plan reviewer is running, or a DAG has ready work. It must not be used as the
sole source of truth for resumption decisions.

## Current Machines

### Runner Lifecycle

This is the only existing explicit transition matrix. It lives in
`src/pal/minion/lifecycle.py`.

| Current | Allowed targets |
| --- | --- |
| `starting` | `running`, `approval_pending`, `clarification_pending`, `completed`, `failed`, `blocked`, `killed` |
| `running` | `approval_pending`, `clarification_pending`, `completed`, `failed`, `blocked`, `killed` |
| `approval_pending` | `running`, `approval_pending`, `clarification_pending`, `completed`, `failed`, `blocked`, `killed` |
| `clarification_pending` | `running`, `approval_pending`, `clarification_pending`, `completed`, `failed`, `blocked`, `killed` |
| terminal states | no outgoing transition |

This machine describes process execution only. It cannot answer whether the
work order is waiting for plan acceptance, detail scheduling, DAG dependencies,
or repair.

### Workflow Cursor

`metadata.workflow` is initialized by `op_minion_dispatch_workflow` and carries
profile steps. `resolve_workflow_next()` selects the next profile from two
inputs:

- the artifact's `workflow_next` declaration
- the current resolved profile's output policy

If the requested next profile is not allowed, the workflow is blocked with
`next_profile_not_allowed`. This means an architect-produced plan can request a
coder, but permission to run that coder currently depends on the architect
pack/profile policy. That is the wrong ownership boundary for execution
authorization.

Current workflow step helpers are permissive: callers provide arbitrary status
strings, and there is no workflow transition matrix comparable to
`RUN_STATUS_TRANSITIONS`.

### Staged Architect

Current happy path:

```text
dispatch_workflow
  -> architecture sketch run
  -> sketch_completed
  -> [sketch_only] final compile
     [module_detail] fan out one detail child per sketch module
  -> module_detail_running
  -> module_detail_completed
  -> manager compiles FinalPlanArtifact
  -> final_plan_compiled / workflow.post_gate_pending
  -> plan reviewer
```

Current transition conditions:

| State | Trigger/guard | Result |
| --- | --- | --- |
| initial `stage=architecture_sketch` | sketch run completes with valid stage artifact | `sketch_completed` |
| `sketch_completed` | `planning_depth=sketch_only` | compile final plan |
| `sketch_completed` | `planning_depth=module_detail` | create detail children and set `module_detail_running` |
| detail item `pending`/`waiting_for_slot` | logical slot available | detail item `running` |
| detail item `running` | valid detail artifact | detail item `completed` |
| `module_detail_running` | every expected detail item completed | `module_detail_completed`, then compile |
| compile succeeds | plan store records candidate | `final_plan_compiled`; schedule plan review |
| any stage run fails | terminal post handler | parent `staged_planning=blocked`, workflow and work order blocked |
| final post-processing throws | blocker reason `staged_planning_post_failed` | resumable only when all detail artifacts already completed |

The logical-slot condition-variable scheduler now wakes waiting detail work,
but activation and state transition are still encoded together in manager
control flow.

### Plan Governance and Revision

Current plan-review happy path:

```text
plan candidate
  -> reviewer scheduled (`reviewing`)
  -> pass (`acceptance_pending`)
  -> user/control accepts
  -> acceptance marker written
  -> accepted plan dispatched to coder in the same public operation
```

Review verdict mapping:

| Reviewer result | `plan_review.status` | `next_action` | Work-order projection |
| --- | --- | --- | --- |
| `pass` | `acceptance_pending` | `accept_plan` | `approval_pending` |
| `fail` | `revision_required` | `revise_plan` | `active` |
| `fail` with successful auto revision spawn | `revision_spawned` | effectively wait for revision | `active` |
| `partial` | `human_decision_required` | `human_decision` | `approval_pending` |
| no gate | `gate_missing` | none | `blocked` |
| reconcile exception | `reconcile_failed` | none | `blocked` |

Revision adds another work order and duplicates governance state across the
revision child and source parent:

```text
revision_required/edit_requested
  -> revision child (`revision_in_progress`)
  -> revised candidate submitted and reviewed
  -> passing candidate copied/projected to source parent
  -> source parent `acceptance_pending`
```

The plan store correctly gives plan identity stronger semantics than the work
order fields:

- candidate identity is `(task_id, plan_id, plan_revision, sha256)`
- an unaccepted revision may be atomically replaced during review reconciliation
- an accepted revision cannot be replaced
- acceptance requires a passing bound review gate or explicit human override
- acceptance markers are checked against revision and SHA-256

The remaining problem is orchestration, not plan-file identity.

### Plan Acceptance and Coder Handoff

`op_minion_accept_reviewed_plan` currently performs two separate domain actions:

1. `accept_plan_ref()` writes the immutable acceptance marker.
2. manager RPC `dispatch_accepted_plan` resolves a next profile, constructs the
   plan parent, initializes execution metadata, and spawns it.

The control-action acceptance path does the same. Auto-accept in the review
orchestrator also accepts and dispatches immediately.

`dispatch_accepted_plan` then:

1. loads the accepted plan;
2. resolves `workflow_next` against the producer pack;
3. updates workflow and plan-review metadata;
4. builds a plan-parent pack;
5. initializes `plan_execution`;
6. spawns the plan parent and ready module children.

This coupling explains the observed friction where the plan was valid and
accepted but coder startup failed with `next_profile_not_allowed`. Acceptance
should remain successful even when execution admission or startup fails.

### Coder DAG

The DAG transition logic in `src/pal/minion/dag_advancer.py` is the cleanest
domain component in the current implementation. It normalizes topology and
computes readiness from dependency completion.

Parent status is derived mechanically:

| Condition | `plan_execution.status` |
| --- | --- |
| every node completed | `completed` |
| at least one running child | `running_module` |
| at least one ready/repair/stale node can start | `awaiting_continue` |
| no running or ready node and not complete | `blocked` |

Node transitions currently implemented by the advancer are:

| Action | Before | After |
| --- | --- | --- |
| initialize | dependencies complete | `ready` |
| initialize | dependencies incomplete | `blocked` |
| claim ready node | `ready`, `needs_repair`, or `stale` | `running` with child work-order id |
| complete node | `running` with matching child id | `completed`; downstream indegrees recomputed |
| release nonterminal child | `running` | `ready` |
| release terminal failure | `running` | `blocked` |
| resume | `blocked`, `failed`, or `paused` with indegree zero | `ready` |
| apply repair replay to target | any affected completed/current state | target `needs_repair`; downstream `stale` or dependency-blocked |

The repository wraps these transitions with optimistic metadata writes and owns
workspace integration. The manager owns scheduling and spawning. A module child
executes serial milestones; each claimed checkpoint is reviewed. A passing gate
creates a durable `completed` checkpoint, advances the next milestone, and
eventually calls `record_plan_module_completion()` on the parent.

One step-executor process may host several runner coroutines. Therefore multiple
active coder modules can legitimately report the same process PID; the run id
and child work-order id are the execution identities.

### Recovery Paths

Current recovery is imperative and path-specific:

- manager startup/reconcile marks lost runner processes failed or killed;
- `recover_work_order` finds `running_module` entries without active child runs,
  releases them, then calls module resume;
- `resume_work_order` first attempts one special staged-planning post failure,
  then module resume, then generic workflow-step resume;
- `retry_checkpoint_review` repairs reviewer scheduling separately;
- `tick_parent_dag` and automatic ready-DAG ticks activate ready nodes;
- module-detail waiting uses an in-memory condition-variable scheduler;
- repair bills invalidate/replay selected DAG nodes and downstream dependents.

These paths contain useful recovery behavior, but the recoverable state/action
pairs are not declared in one place. A work order can therefore appear `active`
while no scheduler recognizes its nested state.

## Current Capability Inventory

Capabilities are listed by responsibility. Canonical names are the stable API;
manager RPC names are internal socket methods.

### Body/Public Introspection

- `intro_module_show`: manager attachment and health summary
- `intro_minion_list`, `intro_minion_read`: live/terminal runs
- `intro_minion_task_search`, `intro_minion_task_read`: task facts
- `intro_minion_work_order_search`, `intro_minion_work_order_read`: durable work-order facts
- `intro_minion_work_order_draft_search`, `intro_minion_work_order_draft_read`
- `intro_minion_profile_list`, `intro_minion_profile_read`
- `intro_minion_plan_search`, `intro_minion_plan_read`

`intro_minion_work_order_read` is the correct source for overall work-order
progress. `intro_minion_read` answers what a particular live runner is doing.

### Body/Public Operations

Lifecycle and configuration:

- `op_minion_attach`, `op_minion_detach`
- `op_minion_configure`, `op_minion_flush_runtime_config`
- `op_minion_kill`, `op_minion_destroy_work_order_run`
- `op_minion_archive_work_order`, `op_minion_remove_work_order`

Tasking and dispatch:

- `op_minion_task_create`
- `op_minion_draft_work_order`, `op_minion_promote_work_order_draft`
- `op_minion_dispatch_workflow`: normal end-to-end entrypoint
- `op_minion_finalize`

Review and execution control:

- `op_minion_review_gate_submit`
- `op_minion_accept_reviewed_plan`: currently accept **and** dispatch
- `op_minion_request_reviewed_plan_revision`
- `op_minion_tick_parent_dag`
- `op_minion_submit_repair_bill`
- `op_minion_recover_work_order`
- `op_minion_resume_work_order`
- `op_minion_pause_work_order`

There is currently no first-class public capability that takes an external,
schema-valid plan candidate through validation, review/acceptance, and later
execution without pretending it came from the architect workflow.

### Manager Socket Methods

Runtime/process methods:

- `health`, `reload_runtime_config`, `list_runs`, `read_run`, `spawn`, `kill`,
  `shutdown`
- `send_decision`, `send_clarification`
- `request_logical_slot`, `wait_logical_slot`, `release_logical_slot`
- `destroy_work_order_run`

LLM broker methods:

- `llm_preflight`, `llm_generate`, `llm_generate_stream`
- `llm_resolve_max_output_tokens`, `llm_resolve_endpoint_facts`

Workflow and DAG methods:

- `finalize_work_order`
- `dispatch_accepted_plan`, `dispatch_plan_revision`
- `tick_parent_dag`, `submit_repair_bill`
- `recover_work_order`, `resume_work_order`, `retry_checkpoint_review`
- `pause_work_order`, `finish_work_order`

### Minion-Scoped Capability Groups

All roles also receive selected read/research/artifact groups. The orchestration
relevant groups are:

| Group | Purpose |
| --- | --- |
| `core_minion_read` | Minion-local facts and artifacts |
| `workspace_read` | tree/search/file/git read access |
| `web_research` | web search/read where permitted |
| `minion_plan_reader` | read/find/get/validate plan nodes |
| `minion_plan_builder_sketch` | sketch topology, module contracts, module AC, gates, constraints, decisions, submit sketch |
| `minion_plan_builder_module_detail` | bound-module interfaces, milestones, AC, validate and submit detail |
| `minion_plan_builder_revision` | read and locally revise an existing plan; excludes begin/finalize |
| `minion_review_gate` | submit plan/checkpoint/gate-contract review results |
| `code_work` | file mutation, git, shell, checkpoint commit |
| `minion_checklist` | milestone checklist read/update |
| `minion_repair_bill_builder` | structured local replay or architecture-defect escalation |

Sketch builder surface:

- read/get/validate/checkout
- add/update/delete gate checks
- add/update/delete constraints and design decisions
- add one or batch sketch module outlines
- add module interfaces and module-level AC
- update/delete/merge sketch modules
- submit sketch

Module-detail builder surface:

- plan read/find/get/validate
- add a bound-module interface
- add milestone outlines and milestone AC
- update/delete milestones and AC; replace milestone AC
- validate and submit the module detail

Repair-bill builder surface:

- begin bill
- add module patch or module entry
- add acceptance criteria and evidence
- validate and submit

## Current Ownership Problems

These are state-machine defects rather than isolated bugs.

1. **No single aggregate state**. Several metadata subdocuments can disagree,
   and `work_order.status` hides the disagreement.
2. **Acceptance and dispatch are coupled**. A valid acceptance can be followed
   by dispatch failure in the same operation, obscuring what succeeded.
3. **Producer policy authorizes consumer execution**. `workflow_next` is checked
   against the resolved architect pack rather than an execution policy owned by
   the work order/task/control plane.
4. **Parent and revision child duplicate governance state**. Reconciliation
   writes both records and can leave different refs, hashes, or next actions.
5. **Transitions and effects are interleaved**. State mutation, child creation,
   process spawn, event delivery, and user notification occur in one call path.
6. **Recovery is state guessing**. Resume code inspects nested dictionaries and
   tries several procedures instead of dispatching one declared action.
7. **External plans are not a first-class entrypoint**. Plan storage can read and
   validate refs, but public orchestration assumes architect provenance.
8. **String states are open-ended**. Most machines have no enum or transition
   validator, so similar meanings accumulate under different names.
9. **User notification is too early in places**. Candidate submission can look
   complete before reviewer reconciliation and human acceptance are finished.

## Matrix Target

The refactor should use an explicit transition definition, for example:

```python
TRANSITIONS: dict[tuple[State, Action], Transition] = {
    (State.CANDIDATE, Action.START_REVIEW): Transition(
        target=State.REVIEWING,
        guard=has_valid_candidate,
        effects=(Effect.SCHEDULE_REVIEWER,),
    ),
}
```

`Transition` should declare the target, guard, persisted domain events, and
effects. An effect runner performs spawn, notification, or file operations only
after the state/event write commits. Repeated actions must be idempotent.

### Machine A: Architect Producer

The producer owns only the creation of a candidate. It does not accept plans or
authorize/start coders.

States:

```text
IDLE
SKETCH_RUNNING
SKETCH_READY
DETAIL_RUNNING
DETAIL_READY
COMPILING
CANDIDATE_READY
BLOCKED
FAILED
CANCELLED
```

Primary transition matrix:

| State | Action | Guard | Next state | Effect |
| --- | --- | --- | --- | --- |
| `IDLE` | `START_SKETCH` | requirements contract exists | `SKETCH_RUNNING` | spawn sketch run |
| `SKETCH_RUNNING` | `SKETCH_SUCCEEDED` | valid sketch ref | `SKETCH_READY` | persist sketch ref |
| `SKETCH_RUNNING` | `RUN_FAILED` | terminal failure | `BLOCKED` | record blocker |
| `SKETCH_READY` | `START_DETAILS` | detail depth selected | `DETAIL_RUNNING` | create detail slots/children |
| `SKETCH_READY` | `START_COMPILE` | sketch-only selected | `COMPILING` | compile candidate |
| `DETAIL_RUNNING` | `DETAIL_SUCCEEDED` | bound module and valid ref | `DETAIL_RUNNING` or `DETAIL_READY` | persist one detail result |
| `DETAIL_RUNNING` | `DETAIL_FAILED` | retry policy exhausted | `BLOCKED` | record module blocker |
| `DETAIL_READY` | `START_COMPILE` | every required module complete | `COMPILING` | compile candidate |
| `COMPILING` | `COMPILE_SUCCEEDED` | valid candidate ref | `CANDIDATE_READY` | emit `PlanCandidateSubmitted` |
| `COMPILING` | `COMPILE_FAILED` | validation/compiler error | `BLOCKED` | record deterministic error |
| `BLOCKED` | `RETRY` | blocker declares retry transition | prior declared retry state | enqueue only declared effect |

`CANDIDATE_READY` is terminal for this machine. Plan Governance consumes its
event. An externally supplied plan bypasses this machine entirely.

### Machine B: Plan Governance

This machine is the sole owner of candidate review, revision lineage, and
acceptance.

States:

```text
CANDIDATE
REVIEW_QUEUED
REVIEWING
ACCEPTANCE_PENDING
REVISION_REQUIRED
REVISION_RUNNING
HUMAN_DECISION_REQUIRED
ACCEPTED
REJECTED
BLOCKED
```

Primary transition matrix:

| State | Action | Guard | Next state | Effect |
| --- | --- | --- | --- | --- |
| `CANDIDATE` | `QUEUE_REVIEW` | schema and SHA valid | `REVIEW_QUEUED` | enqueue reviewer |
| `REVIEW_QUEUED` | `REVIEW_STARTED` | reviewer lease acquired | `REVIEWING` | bind review run |
| `REVIEWING` | `REVIEW_PASSED` | gate targets exact candidate SHA | `ACCEPTANCE_PENDING` | notify user/control |
| `REVIEWING` | `REVIEW_FAILED` | actionable findings exist | `REVISION_REQUIRED` | persist edit allowlist |
| `REVIEWING` | `REVIEW_PARTIAL` | user-owned decision required | `HUMAN_DECISION_REQUIRED` | notify user |
| `REVISION_REQUIRED` | `START_REVISION` | revision policy allows | `REVISION_RUNNING` | spawn revision against source ref |
| `REVISION_RUNNING` | `REVISION_SUBMITTED` | lineage and revision valid | `CANDIDATE` | replace governance candidate, queue review |
| `ACCEPTANCE_PENDING` | `ACCEPT` | pass gate or explicit override | `ACCEPTED` | write immutable marker only |
| `ACCEPTANCE_PENDING` | `REQUEST_EDIT` | edit instruction nonempty | `REVISION_REQUIRED` | persist exact edit scope |
| decision states | `REJECT` | authorized actor | `REJECTED` | notify and close governance |
| recoverable state | `EFFECT_FAILED` | state commit already succeeded | same state with pending effect | retry effect idempotently |

The governance record should live once, on the source work order or a dedicated
plan-governance aggregate. Revision runs are workers referenced by that record;
they should not own a competing copy of governance truth.

### Machine C: Plan Execution

This machine starts only from an immutable `AcceptedPlanRef`.

States:

```text
NOT_STARTED
STARTING
RUNNING
PAUSED
REPAIR_PENDING
BLOCKED
COMPLETED
FAILED
CANCELLED
```

Primary transition matrix:

| State | Action | Guard | Next state | Effect |
| --- | --- | --- | --- | --- |
| `NOT_STARTED` | `START` | accepted marker valid and executor policy authorizes plan | `STARTING` | initialize DAG and workspace |
| `STARTING` | `STARTED` | DAG persisted | `RUNNING` | schedule ready nodes |
| `STARTING` | `START_FAILED` | deterministic setup error | `BLOCKED` | preserve accepted plan and error |
| `RUNNING` | `NODE_SUCCEEDED` | child lease matches node | `RUNNING` or `COMPLETED` | advance DAG and schedule readiness |
| `RUNNING` | `NODE_FAILED` | child lease matches node | `REPAIR_PENDING` or `BLOCKED` | apply failure policy |
| `RUNNING` | `PAUSE` | authorized | `PAUSED` | stop new claims; cooperative cancel optional |
| `PAUSED` | `RESUME` | accepted plan/workspace still valid | `RUNNING` | schedule ready nodes |
| `REPAIR_PENDING` | `APPLY_REPAIR` | valid repair bill | `RUNNING` | invalidate target/downstream nodes |
| nonterminal | `CANCEL` | authorized | `CANCELLED` | cooperative cancellation |

`ACCEPT` is deliberately absent. `START` is a separate public/control action.
Failure to start execution cannot undo or rewrite plan acceptance.

### Machine D: DAG Node

States:

```text
BLOCKED_BY_DEPS
READY
CLAIMED
RUNNING
CHECKPOINT_REVIEW
REPAIRING
SUCCEEDED
FAILED
PAUSED
STALE
CANCELLED
```

Primary transition matrix:

| State | Action | Next state |
| --- | --- | --- |
| `BLOCKED_BY_DEPS` | `DEPENDENCIES_SATISFIED` | `READY` |
| `READY` | `CLAIM` | `CLAIMED` |
| `CLAIMED` | `WORKER_STARTED` | `RUNNING` |
| `CLAIMED` | `LEASE_EXPIRED` | `READY` |
| `RUNNING` | `CHECKPOINT_SUBMITTED` | `CHECKPOINT_REVIEW` |
| `CHECKPOINT_REVIEW` | `GATE_PASSED_WITH_MORE_WORK` | `RUNNING` |
| `CHECKPOINT_REVIEW` | `GATE_PASSED_COMPLETE` | `SUCCEEDED` |
| `CHECKPOINT_REVIEW` | `GATE_REQUESTED_REPAIR` | `REPAIRING` |
| `REPAIRING` | `REPAIR_SUBMITTED` | `CHECKPOINT_REVIEW` |
| active state | `ROLE_FAILED` | `FAILED` |
| completed/downstream state | `INVALIDATE_FOR_REPLAY` | target `READY`, dependent `STALE` |
| `FAILED`/`PAUSED`/`STALE` | `RESUME` with dependencies satisfied | `READY` |

Parent execution status must be a pure projection of node states. Scheduling
must never directly invent a parent state independent of the node matrix.

## Target Capabilities

The next public surface should make domain actions explicit while retaining the
normal convenient workflow entrypoint.

| Capability | Responsibility |
| --- | --- |
| `op_minion_dispatch_workflow` | Create work order and optionally start its configured producer. |
| `op_minion_submit_plan_candidate` | First-class entry for an external or programmatically generated valid plan candidate. |
| `op_minion_request_reviewed_plan_revision` | Apply `REQUEST_EDIT`/`START_REVISION`; no execution side effects. |
| `op_minion_accept_reviewed_plan` | Apply governance `ACCEPT` only. |
| `op_minion_start_plan_execution` | Start coder DAG from an accepted plan ref. |
| `op_minion_pause_work_order` | Apply execution `PAUSE`. |
| `op_minion_resume_work_order` | Resolve aggregate kind, then dispatch one declared `RESUME` action. |
| `op_minion_submit_repair_bill` | Apply execution/node repair transition. |
| `op_minion_archive_work_order` | Administrative projection/lifecycle, not a substitute for cancellation. |

For ergonomic autonomous mode, a controller may call `ACCEPT` and then `START`
back-to-back, but they remain two persisted transitions with independently
observable outcomes.

Executor authorization should come from task/work-order execution policy, not
from `FinalPlanArtifact.metadata.workflow_next` and not from the architect
profile. The artifact may recommend an executor profile; the control plane
decides whether it is allowed.

## Hard Invariants

The matrix implementation should enforce these before migration is considered
complete:

1. Every business state field has exactly one owning machine and one transition
   function.
2. `work_order.status` is derived from machine state; callers do not use it to
   infer detailed progress.
3. A plan candidate is identified by revision and SHA-256. A review gate targets
   that exact identity.
4. Accepted plan content is immutable.
5. Plan execution requires an `AcceptedPlanRef`; producer completion is not
   sufficient.
6. Accepting a plan never starts execution in the same transition.
7. An architect/revision worker can emit a candidate but cannot accept it or
   authorize an executor.
8. A node can be `RUNNING` only with one current child lease/run identity.
9. A stale child completion cannot advance a node.
10. DAG readiness is derived from completed dependencies and node states.
11. Revision creates a new candidate lineage entry from an immutable source;
    only an unaccepted same-revision review candidate may be atomically replaced.
12. Every side effect has an idempotency key and durable pending/completed state
    or an outbox record.
13. Restart recovery scans pending effects and leases; it does not guess which
    ad hoc resume procedure might apply.
14. User-facing “ready to review” notification occurs after reviewer
    reconciliation, not merely after architect submission.

## Migration Order

1. Define typed enums, actions, transition results, and projection functions
   without changing external behavior.
2. Make Plan Governance the first matrix machine. Split acceptance from
   dispatch and add the external candidate entrypoint.
3. Move architect sketch/detail/compile orchestration into the Producer matrix.
   Keep the current plan-builder artifacts and compiler.
4. Wrap existing `dag_advancer.py` functions as DAG Node matrix transitions;
   preserve its dependency calculations.
5. Add the Plan Execution matrix and make manager scheduling consume persisted
   transition effects.
6. Replace nested `resume_work_order` conditionals with aggregate/action
   dispatch and explicit retryable effects.
7. Turn `work_order.status`, workflow summaries, notifications, and pager output
   into projections from the authoritative machines.
8. Remove old direct metadata writes after parity tests and restart/recovery
   tests pass. No compatibility state machine should remain.

## Test Baseline for the Refactor

At minimum, matrix tests should cover:

- every declared `(state, action)` pair and every rejected pair
- duplicate action idempotency
- process death between state commit and effect execution
- manager restart with pending detail, review, revision, and DAG effects
- exact plan/review SHA binding
- acceptance success followed by execution-start failure
- external plan candidate through review, acceptance, and execution
- revision pass updating one governance aggregate only
- stale child completion and stale DAG revision rejection
- parallel ready nodes sharing a step-executor process but retaining distinct
  run/lease identities
- checkpoint pass, checkpoint repair, reviewer loss, and persisted review retry
- repair replay invalidating target and downstream nodes only
- projection consistency for `work_order.status` and user notifications

The desired end state is not fewer checks. It is the same rigor with one owner
for each transition and a mechanical answer to: given this state and action,
what state comes next and which idempotent effects must run?

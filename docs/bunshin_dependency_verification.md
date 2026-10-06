# Software dependency verification and repair

## Binding policy

New `software_git.v2` GraphIR generations classify every declared dependency on a
produced module as an EXECUTION edge. A consumer's implementation remains runnable
from the approved interfaces without waiting for provider implementation. Its
verifier waits for accepted provider products and receives those exact products in
its assembled candidate. True `contract_only` modules remain contract/interface
inputs and never become executable provider repair targets. The declared sink also
waits for every produced module, including synthetic checker dependencies.

GraphIR generations are immutable. Loading an existing generation does not change
its edge kinds or dependency bindings. Resolving a terminal UNKNOWN on an old
contract-only intermediate graph does not retroactively assemble provider products.

## Durable concurrent provider repair

A validated dependency report prepares immutable report, packet and corpus refs.
It registers a generation-local cohort for the actual provider/consumer closure;
a shared sink alone does not combine unrelated outside reports. Overlapping
captured reports may expand the actual closure before atomic apply.

The graph ledger fences admission and publication while its finite admitted
frontier is drained. For every original member, Manager must:

1. Retire and join the exact setup task and process incarnation
2. Close submission authority under the role-submission write lock, preserving a
   late RESULT_RECORDED receipt even if no Pending artifact was created
3. Validate and preserve all submitted findings and verifier-authored corpus;
   invalid scope produces a correction obligation, never an invented provider
4. Release the exact worktree ownership and original/rebound business leases
5. Persist the capture refs and complete closure proof before acknowledging STALE

Actual recovered-attempt business ownership is stored as an immutable
`RoleAttemptBusinessLeaseArtifact`. An assignment's initial execution spec, the
current attempt's business lease, the role-attempt access lease and a later
snapshot lease are distinct identities. Setup before claim uses the current
aggregate tuple under the graph admission lock. Cleanup cannot substitute a later
worker, lease or attempt for the captured one.

Atomic apply re-reads the current cohort under the write transaction. All validated
provider owners reopen together; the remaining consumers become stale. Captured
PASS/UNKNOWN reports receive explicit invalidated receipts instead of accepting an
obsolete binding. Historical repair packets and corpus checkpoints remain
mandatory input to later work views and case resolution. Applied/superseded cohort
history, rather than current mutable packet pointers, controls replay.

A validated routed dependency failure appends one entry to the existing no-progress
history. The third identical failure stops both initiating and captured reports in
terminal no-progress triage. A captured peer is stopped before registering its new
provider targets. Ordinary triage resolution cannot waive that committed terminal
receipt, even if a later cleanup error overwrites the visible blocker.

## Recovery and control

A REGISTER outbox effect carries exact capture/incarnation refs and graph
generation. Its authority outlives the source's temporary aggregate cursor, so a
failure after source closure to STALE remains retryable. Replaying an applied old
effect cannot operate a later cohort. Bounded outbox exhaustion remains visible.

Recoverable node triage restores only the logical cursor bound by the current
frozen frontier and durable evidence. It does not start a worker, claim a lease,
change the incarnation or consume a new role budget. If apply exhausts after all
members have closed, normal workflow triage resolution invokes the persisted
REGISTER authority through the fresh workflow recovery effect; the old failed
outbox attempt history remains unchanged.

Pause is an aggregate/workflow control overlay. Exact cleanup may finish while
paused, but capture/close/apply wait for explicit resume. The frozen graph cursor
and all evidence remain bound to the same incarnation. Terminal cancellation and
replan supersede the old cohort and prohibit its provider reopen.

## Existing workflow upgrade

Use the existing architecture replan/review path to obtain a freshly compiled
GraphIR generation. A truthful architecture/policy correction must be submitted
and accepted through that path; unchanged accepted leaves can be reused, while
changed consumers are invalidated according to the existing generation diff.
Preserved worktrees are implementation starting points, not permission to skip
fresh verification. Existing replan reuse restrictions still apply to non-leaf
accepted modules.

If the current workflow cannot enter supported replan, preserve it and create a
replacement through existing workflow controls. A bare restart/import reuses the
old architecture artifact and does not recompile its GraphIR. Its supported edit
and architect-resubmission path is required before accepting the replacement.
There is no in-place edge rewrite or automatic migration API in this change.

## Verification boundaries

`DependencyRepair*.tla` and the bounded Python model explorers document protocol
and control correspondence. Python exploration and expected negative controls do
not establish a universal liveness or compositional proof. TLC requires an
existing pinned `TLA2TOOLS_JAR`; an optional skip is not a TLC pass.

The runtime regression suites separately exercise compiled software graphs, real
repository transactions, claimed public outbox effects, real process/task/worktree
cleanup, late receipts, lease rebinding, replay, explicit control and failure
budgets. Those integration tests are the release gate for the source refinement.

### Imported architecture restart lifecycle

An external architecture import binds its exact durable product to an initial
PlanCycle with the explicit `IMPORT_PRODUCT` action. This makes it checker-ready
without inventing an Architect assignment or accepting its embedded graph. The
revision import, workflow pointer and plan binding commit together. A legacy
import stranded before this binding recovers through ordinary architecture triage
resolution; reviewer admission validates the immutable import event, current
request, product and pinned requirements before restoring that binding.

Software review of a foreign-workflow artifact uses a disposable workspace made
from its durable Git bundle, so cleanup of the old workflow does not remove its
review input. Human Accept rejects a foreign GraphIR before consuming the decision
token; Human Edit stays available. Edit then gives the Architect a replacement-
owned worktree in the same project, and normal submission compiles the fresh graph.
The edited PlanCycle can be generation 2 while the replacement workflow's first
GraphIR is generation 1. The old artifact, graph and source workflow remain intact.

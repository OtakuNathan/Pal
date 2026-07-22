# Minion V2 Contract-Driven Orchestration

Status: active implementation as of 2026-07-21.

Minion V2 is a clean workflow cutover. V1 plans, milestones, cursors,
checkpoints, write RPCs, and workflow resume paths are not accepted by V2.
Legacy tables remain readable for diagnosis and archive tooling only.

## Business Aggregates

All business mutation enters through `ActionEnvelope` and the table-driven
transition engine in `src/pal/minion/v2/`. Reducers are pure. A committed
transition atomically performs snapshot CAS, domain-event append, action
deduplication, outbox insertion, and projection update for one aggregate.

The six aggregate types are Task, Workflow, Architecture Revision, Execution
Epoch, DAG Node Run, and Standalone Review. Workflow phase is a query projection
derived from child aggregate state. It is not a second writable state field.

## Durable Effects And Roles

Network delivery, process admission, channel delivery, Git publication, and
cross-aggregate action submission are outbox effects with at-least-once
delivery. Each effect is keyed by its causative event and index. Effect receipts
and action idempotency make crash replay safe.

Long LLM work is not an in-flight Outbox attempt. The Outbox effect durably
creates or locates a `RoleAssignment`, starts it in a bounded supervisor, and
ACKs once that assignment exists. The DAG Node Run is the sole owner of module
business lifecycle: coding, verification, repair, acceptance, stale propagation,
pause, cancellation, and triage are Node transitions, never assignment states.

Each Node generation owns one canonical Implementation role session for the
complete module run. `produce` and `repair` are modes of that same role. Each immutable
Candidate or verification-scenario fingerprint owns a separate Verifier role
session. A retry of the same Candidate resumes that session, while a new
Candidate starts a fresh Verifier with no inherited dialogue or tool state.
Historical failures cross Candidate boundaries only through Manager-owned
RepairBills and verification obligations. Node `ACCEPTED` or `CANCELLED` closes
the Coder session; a Verifier session closes after its verdict receipt is
settled. Reopening an accepted Node creates a new role-session generation.

A `RoleAssignment` binds one immutable role/input/effect activation and uses
one explicit receipt protocol:

```text
QUEUED -> CLAIMED -> RUNNING -> RESULT_RECORDED -> SETTLED
                    +---> RETRY_QUEUED -> CLAIMED
```

An attempt binds one subprocess, lease, fencing token, and access token. The
assignment has no pause, repair, acceptance, or triage state. Before a result is
recorded, aggregate control may cancel the activation. After a result is
recorded, even a superseding cancel must settle the receipt rather than discard
it.

Success and failure are symmetric. Worker submit first records an immutable
result receipt. Exhausted or permanent worker failure records a
`RoleAssignmentFailureArtifact` and a `ROLE_FAILED` settlement action. The
corresponding parent Action and assignment settlement then commit in the same
SQLite transaction. Action dedup reconciles a replayed receipt without another
LLM call. Settlement revokes the activation token and lease; a failed attempt
remains `FAILED` even though its assignment receipt is acknowledged.

Manager restart recovers queued, retry-queued, running, or result-recorded
assignments without recreating the causative Outbox effect. Direct stale
transitions also emit an Outbox effect that cancels any not-yet-started
activation, so dependency invalidation does not wait for a later recovery tick.
`role_invocations` is an observability and durable-turn journal; its status is
not consulted as a second business state machine.

The foreground Pal channel loop never waits for a semantic role invocation. Global
worker slots, rather than completed Outbox attempts, enforce LLM concurrency.

Workers hold leases with monotonically increasing fencing tokens. On startup,
an expired worker's recorded process group is terminated and reaped before the
lease is cleared. If the process group or worktree cannot be proven quiet, the
aggregate enters `TRIAGE_REQUIRED`.

## Architecture Contract

The immutable `TaskSourceBundleArtifact` is product truth. It preserves the
user's exact request, supplied source files, examples, qualifications, and
later amendments without extracting normalized Requirement records. For software engineering, the
accepted code-skeleton commit plus the semantic Contract Dependency Graph and
end-to-end Scenario Topology are architecture
truth. For artifact families, the equivalent
architecture is a content-addressed manifest of semantic unit and cross-unit
contracts. Internal IDs and hashes are Manager concerns and are absent from the
LLM authoring surface.

Manager pre-seeds one fixed-schema `architecture.yaml` control-plane Draft for
each Architect invocation. Its `modules` and `scenarios` fields are dynamic maps
keyed by stable semantic names, so initial design and revision use ordinary file
editing rather than per-node mutation tools. A revision starts from the complete
accepted YAML and preserves it across process retries. Architect receives only
the in-place user-question IO and no-argument terminal submit in addition to
ordinary workspace tools. Each module contains its kind, contract dependencies,
contract enforcement mode and paths, writable implementation scopes, and
reference-only paths.
Manager derives one repository verification corpus at `tests/<module_name>/`
for every implementation module; Architect does not declare or name it.
Each scenario names the exact implementation-module combination, real product or
build entrypoint, observable behavior, and environment it verifies.
`file_frozen` is reserved for a physically separate protocol/interface/schema
file that remains Coder-read-only. `review_guarded` is the default when public
shape and implementation share a module-owned file; Manager then binds an
Accepted-Skeleton-to-Candidate contract diff that Verifier must read before
submitting. Cross-module overlap remains invalid in both modes, and the derived
verification corpus can never own contract or reference-only files. Software Architect writes contract-level declarations,
concise adjacent semantic comments where useful, and minimal compile wiring in
an isolated worktree. It may not write algorithms, functional implementations,
milestones, test matrices, implementation checklists, or function-level
construction steps.

Architecture submit performs only deterministic structure and safety checks.
Malformed module/path records, unknown dependencies, cycles, overlapping
writable scopes, missing declared paths, frozen/reference mutation, Git drift,
and unstable snapshots remain blocking. The Manager does not parse comment
chapters, bind task-source coverage, resolve evidence claims, infer consumers,
or judge lifecycle/state/ownership/contract semantics. Architecture Reviewer
receives every immutable task-source file and the skeleton diff and owns all of
those semantic checks.

Research mode is explicit: `none`, `local_only`, or `external_allowed`.
`local_only` removes web capabilities from the resolved role pack; this is
enforced by the Manager rather than prompt text. Architect research is limited
to feasibility and boundary design. Coder handles implementation-local
research from approved references and the repository.

Stateless or stateful behavior is expressed in the skeleton using the target
language's native shape and comments. Its semantic adequacy is Reviewer-owned,
not a Manager schema rule.

## Schema-Bounded Authoring

LLMs do not author canonical submission JSON. Software Architecture authors one
Manager-preseeded YAML projection whose fixed schema is validated and compiled
into the canonical immutable manifest only by `architecture_submit`. Candidate,
verification, and standalone-review roles receive narrow semantic mutation or
execution tools. The SWE Verifier writes executable tests and ends
with one outcome tool carrying at most prose findings and semantic module
names; it never maintains case/finding/evidence records. The Manager validates
the live workspace, derives hidden identities and Git deltas, and materializes
the canonical artifact before allowing the worker to exit.

Authoring Drafts are durable, lease-fenced, versioned, and operation-idempotent.
The Architecture YAML is an author-visible file projection, not an aggregate or
workflow truth source; submit rechecks the active lease, fencing token, complete
schema, semantic graph, revision scope, Git state, and snapshot stability before
advancing the state machine. Duplicate YAML keys, aliases, custom tags, merges,
unknown fields, and stale invocations are rejected.
A replacement role invocation with the same immutable input fingerprint inherits only
semantic definitions by default. Every family Verifier uses one semantic
outcome and restarts from immutable task sources, candidate or artifact diff,
durable review-scratch probes, and prior Repair Packets. It never inherits an
LLM-maintained case/finding database. Standalone review keeps its separate
review-only Draft because it produces a report rather than a DAG-node verdict.
Every authoring tool schema is bounded to at most 12 top-level properties and
depth four, with no arrays of objects, schema-valued `additionalProperties`, or
`oneOf`/`anyOf`. Manager-owned identity fields such as IDs, refs, hashes,
handles, and JSON pointers are rejected from role authoring schemas. Old
monolithic builder and revision-read capabilities are not compatibility aliases.

Architecture Reviewer receives every immutable task-source file that Architect
received, every module and scenario, the complete skeleton diff, and prior
findings. It independently checks
source-obligation preservation and coverage, contracts, consumers, ownership,
lifecycle/state/invariants, implementation leakage, and end-to-end
reachability. It records each material defect through `add_finding` with a stable
semantic key, p0/p1/p2 priority, summary, and optional structured source locations,
preferably batching independent calls in one tool round. It then submits once:
PASS with an empty finding Draft or FAIL with the structured Draft. It never emits
Markdown as its machine contract or mirrors the input as positive audit rows.

Sandboxed role processes cannot mount Minion's database or content-addressed store.
They receive only an assignment-scoped gateway endpoint and an opaque attempt
token. Immutable semantic inputs are materialized under named read-only
reference roots and read with ordinary file/search tools; no receipt protocol
is required for reading them. The gateway exposes fenced Draft
mutation/submission, artifact publication, and the LLM broker. A token is checked against its active
attempt lease and may use only the broker run owned by that assignment session.

Pal's main SQLite database, WAL/SHM files, and configuration are mounted
read-only so ordinary memory recall remains available. The slim worker runtime
opens SQLite with `mode=ro` and `query_only`, skips schema/default provisioning,
does not refresh memory indexes, and does not increment recall usage counters.

Developer and verifier checks execute when their dedicated tool is called.
Command, environment, stdout, stderr, status, and LSP diagnostics are persisted
as immutable evidence before the Draft advances. An unchanged check can reuse
that evidence; submit never asks the Manager to reinterpret or rerun a
model-authored test-plan JSON. Artifact producers may write their product
files, but `producer_report.json` is Manager-owned and generated only by
`candidate_submit`.

Workspace preparation selects the LSP environment from the task's declared
primary language. Repository language discovery is a fallback and does not
automatically activate servers for fixture or reference languages; secondary
languages require an explicit declaration. Each language adapter must bind a
real project model or a declared fallback context before prewarm. Generated
context lives in Manager-owned runtime storage, never in the candidate
worktree. LSP evidence records that context and its fidelity, and a server that
can start without a usable project context is still unavailable for semantic
verification.

## Human Governance

Architecture Markdown is mechanically compiled from the canonical manifest.
Accept/Edit/Reject cards bind workflow, revision, manifest SHA, actor, channel,
expiry, and a one-use decision token. Token consumption and the architecture
transition are one database transaction. Human Accept marks only the revision
accepted; starting execution is a separate outbox effect.

Human Edit explicitly selects `architecture` or `requirements`. An architecture
edit reuses the current immutable `TaskSourceBundleArtifact`. A requirements
edit appends the user's raw amendment and/or workspace-relative source files,
then publishes a new immutable task-source bundle before consuming the decision
token or creating the child Architecture Revision. Existing source bytes are
never mutated or normalized in place.

Human waivers are immutable artifacts bound to the manifest and relevant
fragment hashes. A changed fragment invalidates the waiver.

Architect clarification is ordinary asynchronous tool IO, not a business state.
The Architecture Revision remains `ARCHITECT_RUNNING` while Manager routes three
inline choices plus a custom-answer path through the active channel. The same
invocation receives the answer, which is persisted as an immutable task-source
amendment and included in the exact source bundle later given to Architecture
Reviewer, Coder, and Verifier.

## Execution and Verification

An accepted manifest compiles to an immutable execution epoch. Contract
dependencies express protocol/data/ownership consumption and must be acyclic,
but they are not Coder start barriers: every implementation Coder starts from
the same Accepted Skeleton and all implementation nodes may compete for slots
immediately. Contract dependency order is retained for semantic handoff,
candidate reuse, impact analysis, and deterministic final union.

Each declared end-to-end scenario compiles to a separate Verification Node. It
waits until exactly the implementation Candidates in its dependency closure are
`ACCEPTED`, assembles their deterministic union, then verifies the declared real
entrypoint, environment, and observable behavior. A scenario owns no product
source. No universal final join is required; a whole-system scenario exists only
when the product has a real whole-system entrypoint. Final publication requires
all required implementation nodes and all declared scenario nodes to be
`ACCEPTED`, with each scenario result matching its current combination
fingerprint.

Coder and Verifier receive the same immutable task-source files plus the
accepted local module skeleton, path policy, semantic contract dependencies,
assumptions, and RepairBills. The protocol surface is already present in the
Accepted Skeleton, so a Coder may implement against another module's accepted
contract before that module's Candidate exists. Verifier also receives a
Manager-generated Git diff from the
Accepted Skeleton to the current Candidate; `review_guarded` modules receive a
contract-path diff, and repair cycles additionally receive the previous-to-current
Candidate delta. They use normal file/search tools against semantic read-only
roots; prompts and independent review enforce reading while Manager avoids a
second receipt state machine. Every implementation module follows the same
Coder-to-Verifier cycle and must have an accepted VerificationArtifact before
final publication. Local progress is a lease-fenced mutable journal, not a
global cursor.

Coder cannot commit. Candidate submission follows:

```text
PRODUCING/REPAIRING -> QUIESCING -> SNAPSHOTTING -> REVIEW_QUEUED
```

Quiescing revokes the role token, stops and reaps the process group, verifies
fencing, and holds an exclusive worktree lock. Manager then checks owned and
reference-only paths, verifies Git HEAD did not move, compares pre/post content
fingerprints, creates the candidate commit, and publishes a candidate artifact.

Module Verifier runs with product code read-only and write authority only over
the Manager-derived `tests/<module_name>/` corpus. Coder sees that corpus
read-only. Scenario Verifier receives a read-only
Manager-assembled Candidate union and writes executable probes/tests only to a
durable review-scratch Artifact. Both derive adversarial cases from contracts,
lifecycle, state, ownership, invariants, and the diff, write real regression
tests, and execute them in an isolated review workspace. Their terminal
tools carry a semantic outcome while every defect is recorded through the same
structured `add_finding` contract; Manager records tool
receipts, snapshots the test delta, computes fingerprints, and owns routing.
Submission follows an explicit durable boundary:

```text
REVIEWING -> REVIEW_QUIESCING -> REVIEW_SNAPSHOTTING -> verdict
VERIFYING -> VERIFY_QUIESCING -> VERIFY_SNAPSHOTTING -> verdict
```

On PASS, Manager promotes the verifier corpus delta into the accepted Candidate.
On FAIL, Manager creates a semantic Repair Packet, installs the verifier corpus
delta read-only in the next Coder worktree, and gives Coder the original findings and
recorded regression commands. Coder repairs product code rather than translating
or rewriting verifier-owned test schemas. Coder has no acceptance authority;
verifier has no product implementation authority.

Module defects return to the same worktree. Dependency defects
reopen the dependency and stale transitive dependents. A contract or
architecture defect is reported to the Execution Epoch; a DAG node never creates
an Architecture Revision directly.

The first such report moves the epoch to `REPLAN_COLLECTING`. Scheduling and new
role admission stop immediately, implementation writers are stopped and made
stale, and only Reviewer/Verifier role invocations that were already running may finish.
After those reviews drain, the manager scans persisted node findings and compiles
one immutable `ArchitectureFindingBatchArtifact`. Equal finding fingerprints are
grouped while retaining every RepairBill/reproducer; different findings of the
same defect kind remain separate. The epoch then enters `REPLAN_REQUIRED` and is
the sole owner allowed to create one deterministic Architecture Revision for
that replan generation. The accepted replacement epoch marks its predecessor
`SUPERSEDED`. Three identical findings with no candidate-tree change enter
triage.

UNKNOWN is nonblocking only with an allowed architecture policy, a complete
assumption reference, and a valid human waiver for hard/security/permission/
public-API semantics.

After architecture replan, accepted module candidates are reused only when the
full contract/environment fingerprint matches: module contract, immutable task
sources, global constraints, owned area, dependency set, dependency
interfaces and outputs, environment policy, and epoch baseline. Reused commits
are imported by the manager; partial matches rerun.

## Sidecar-Owned Profile Catalog

The Minion sidecar exclusively owns profile and family catalog lifecycle.
Package templates are immutable builtins and are loaded directly for every
registry projection; the Wizard and Pal process do not copy, parse, refresh, or
modify them. Runtime state contains only explicit JSON overrides under
`data/minion/catalog/`.

On attach, the sidecar archives legacy `plugins/minion/profiles` and
`plugins/minion/families` TOML seeds before constructing role bindings. Explicit
legacy custom definitions are converted to sidecar-owned overrides; managed
builtin seeds are removed from effective precedence so they cannot mask an
upgraded package template.

Catalog reads, semantic merge patches, resets, and refreshes are manager IPC
operations. Writes are schema-validated, atomically published, audited, and
optionally guarded by catalog generation CAS. Task creation requires one canonical
primary profile, derives its problem-domain Family from that profile, resolves the
Family's exact Architect/Reviewer/Implementation/Verifier bindings, and pins the
complete immutable `FamilyBindingArtifact` on the Task. All workflows inherit that
binding. Catalog changes therefore affect only subsequently created Tasks.

## Public Surface

Pal sees ten workflow capabilities:

- `op_minion_start_workflow`
- `op_minion_submit_artifact`
- `intro_minion_task_search`
- `intro_minion_workflow_status`
- `op_minion_resume_workflow`
- `op_minion_restart_execution`
- `op_minion_resolve_triage`
- `op_minion_submit_human_decision`
- `op_minion_control_workflow`
- `op_minion_archive_workflow`

`resume_workflow` only resumes a deliberately paused workflow. Operator triage is
resolved explicitly with `resolve_triage`, an auditable semantic module/phase
selection and a required resolution summary. The operation dispatches the
aggregate's existing `RESOLVE_TRIAGE` transition; it cannot accept a candidate,
waive verification, or bypass a gate.

`restart_execution` is the explicit replacement path for an execution attempt
whose accepted architecture is still valid. The old workflow first enters
cancel settlement, then a durable replacement effect creates a new
`review_then_execute` workflow for the same Task. The replacement inherits the
Task-pinned Family binding, reruns Architecture Review and Human Review, and never
reuses candidates from the discarded execution. The old workflow becomes
terminal only after the replacement workflow exists, or after a concurrent
restart cancellation has been durably acknowledged.

Catalog administration is also exposed as a thin sidecar proxy:

- `intro_minion_catalog_read`
- `op_minion_catalog_set_profile_override`
- `op_minion_catalog_reset_profile_override`
- `op_minion_catalog_set_family_override`
- `op_minion_catalog_reset_family_override`
- `op_minion_catalog_refresh`

Lease, outbox, scheduler, spawn, tick, and recovery operations are manager-only.
Task search is a cross-channel, actor-scoped Jieba/FTS projection over the
durable Task ledger. Workflow status without a selector remains channel-bound;
an explicit natural-language Task selector resolves through the ledger and may
rebind the uniquely matched workflow to the current channel.

## Persistence

V2 tables use the `minion_v2_` prefix in Minion's SQLite database. Durable
role session, assignment, attempt, invocation, turn, submission, and effect-attempt
records use the same database and transaction boundary. Artifact
bytes live under `data/minion/artifacts/sha256/`; SQLite stores metadata and
reference edges. Publication writes a same-filesystem temporary file, fsyncs,
atomically renames, fsyncs the parent, and only then records durable metadata.
The address includes artifact type, schema version, media type, and bytes, so
identical JSON used for different semantic artifact types cannot overwrite
metadata.

An `AgentSessionContinuationArtifact` is the sole recovery truth for a logical
role session. Before each physical attempt, the Manager resolves the Artifact
reference stored on the session and materializes one explicit continuation
input inside that attempt directory. The runner never scans older run files for
a checkpoint; it writes one explicit output path, which the Manager validates
against session scope, subject, and fencing before publication.

Software repositories live under `data/minion/repos/<project>/`, where the
directory name comes from the explicit project name or source repository name.
Different workflows for one project share `project.git`. Each workflow owns a
readable `minion/<workflow>/main` staging branch; Architect revisions and code
nodes use separate branches and worktrees beneath that workflow namespace.
Only the manager advances the workflow branch after accepted verification.
The source repository and its target branch are not modified until a separate
explicit publish decision. Architecture review uses a temporary detached
worktree, and reviewed plans are materialized under
`data/minion/plan_revisions/` as a read projection rather than a second truth.

The status projection always reports one current phase, active aggregate and
worker, blocker, legal next actions, user wait, cumulative metrics, last event,
and liveness. Deliberate `PAUSED`, human wait, and operator/triage wait are
valid quiet states; an otherwise orphaned workflow is moved to triage.

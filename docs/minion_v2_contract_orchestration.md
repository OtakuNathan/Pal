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

The immutable `TaskLedgerArtifact` is product truth. The foreground Pal compiles
the complete initial request into the ledger's structured `original` value.
Ordered `revisions` are append-only and each carries the exact user question and
answer observed by Manager. Apply them in sequence: a newer revision takes
precedence when its meaning conflicts with earlier revisions or `original`, while
all unrelated earlier obligations remain binding. Manager validates the fixed
schema and appends the communication mechanically; it never asks a role to
compile, paraphrase, patch, or restate the task. The only role projection is one
read-only `task.yaml`; there is no parallel request, amendment, or compiled-task
document. For software engineering, the
accepted code-skeleton commit plus the semantic Contract Dependency Graph and
end-to-end Scenario Topology are architecture
truth. For artifact families, the equivalent
architecture is a content-addressed manifest of semantic unit and cross-unit
contracts. Internal IDs and hashes are Manager concerns and are absent from the
LLM authoring surface.

Manager pre-seeds one fixed-schema `architecture.yaml` control-plane Draft for
each Architect invocation. Its `requirements`, `modules`, and `scenarios` fields
are dynamic maps keyed by stable semantic names, so initial design and revision
use ordinary file editing rather than per-node mutation tools. A revision starts
from the complete accepted YAML and preserves it across process retries.
Architect receives the in-place user-question IO and no-argument architecture
submit in addition to ordinary workspace tools. `ask_question` suspends until
Manager has appended the exact question and answer to the ledger; Architect then
continues directly and has no task-ledger write capability. The Draft uses
schema version 4.

`requirements` is a compact mapping index. Each entry has one claim, one
module-or-scenario owner, and an ordered public semantic `contract_path`; it does
not duplicate module state or test partitions. Each module is a complete Module
Protocol: kind, behavior kind, one responsibility, provider dependencies with
the exact provider outputs consumed plus purpose and handoff, contract inputs,
outputs, errors and invariants, ownership rules, a closed lifecycle, an optional
reachable state machine, and physical path policy. `architecture.yaml` is the
canonical module-level semantic definition. Target-language declarations and
adjacent comments provide the symbol-level contract; disagreement is an
architecture defect.

Each scenario names the exact implementation-module combination, requirement
mappings it consumes, real product or build entrypoint, ordered contract flow,
observable success behavior, legal failure behavior, and environment. Manager
rejects missing/unknown owners and scenario references, unconsumed requirements,
cycles, and dependency edges that consume undeclared provider outputs before
snapshotting the skeleton. For every implementation module, Manager derives two
durable repository corpora: Coder-owned `tests/<module_name>/developer` and
Verifier-owned `tests/<module_name>/verification`. Architect declares neither.
`file_frozen` is reserved for a physically separate protocol/interface/schema
file that remains Coder-read-only. `review_guarded` is the default when public
shape and implementation share a module-owned file; Manager then binds an
Accepted-Skeleton-to-Candidate contract diff that Verifier must read before
submitting. Cross-module overlap remains invalid in both modes, and neither
derived test corpus can own contract or reference-only files. Software Architect writes contract-level declarations
and concise adjacent semantic comments where useful in an isolated worktree. It
may name required external libraries or runtimes, but build and test machinery
belongs to implementation and verification. It may not write algorithms, functional implementations,
milestones, test matrices, implementation checklists, or function-level
construction steps.

Architecture submit performs deterministic protocol, graph, and safety checks.
Malformed module/path records, incomplete state graphs, unknown dependencies or
consumed outputs, cycles, overlapping
writable scopes, missing declared paths, frozen/reference mutation, Git drift,
and unstable snapshots remain blocking. Manager validates declared shape and
reference closure but does not judge whether prose semantics are correct,
whether declaration comments agree in meaning, or whether a contract graph
actually satisfies product intent. Architecture Reviewer
receives the immutable task.yaml ledger and the skeleton diff and owns all of
those semantic checks, including auditing every revision against its embedded
question and answer.

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
execution tools. The SWE Verifier writes executable tests, records failures
through structured `add_finding` calls, and ends with one semantic outcome tool;
it never maintains a separate case/finding/evidence manifest. The Manager validates
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
outcome and restarts from the immutable task ledger, candidate or artifact diff,
durable review-scratch probes, and prior Repair Packets. It never inherits an
LLM-maintained case/finding database. Standalone review keeps its separate
review-only Draft because it produces a report rather than a DAG-node verdict.
Dynamic topology stays in the fixed-schema YAML rather than per-node tools.
Terminal tool schemas are compiled from the bound topology, use strict bounded
Pydantic objects, and expose no schema-valued `additionalProperties` or
`oneOf`/`anyOf`. Manager-owned identity fields such as IDs, refs, hashes,
handles, and JSON pointers are rejected from role authoring schemas. Old
monolithic builder and revision-read capabilities are not compatibility aliases.

Architecture Reviewer receives the same immutable task.yaml ledger as Architect,
every requirement mapping, complete Module Protocol, scenario, skeleton diff,
and prior finding. It independently checks requirement preservation, protocol
completeness, declaration-comment agreement, dependency handoffs, ownership,
lifecycle/state/invariants, implementation leakage, and end-to-end success and
failure reachability. The requirement mapping is an audit index, not proof by
assertion. It records each
material defect through `add_finding` with a stable
semantic key, p0/p1/p2 priority, summary, and optional structured source locations,
preferably batching independent calls in one tool round. It then submits once:
PASS with no arguments and an empty finding Draft, or FAIL with the structured
Draft. It never emits Markdown as its machine contract.

Each module work view contains the full Module Protocol, direct dependency
definitions and edges, direct consumer edges, relevant requirements and
scenarios, both test scopes, dependency availability, RepairBills, and the
durable node journal. Coder and Verifier derive tests from those semantics and
store executable cases in their respective corpora. `verification_pass` has no
manually maintained coverage payload; Manager mechanically requires a
Verifier-owned test delta and a successful final shell or LSP receipt after the
last edit.

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
edit reuses the current immutable `TaskLedgerArtifact`. A requirements edit
records the exact human amendment as a Manager-authored revision, produces the
next immutable ledger generation, and creates a child Architecture Revision
against that generation. Manager never rewrites `original` or paraphrases the
answer, and Architect cannot edit the ledger.

Human waivers are immutable artifacts bound to the manifest and relevant
fragment hashes. A changed fragment invalidates the waiver.

Architect clarification is ordinary asynchronous tool IO, not a business state.
The Architecture Revision remains `ARCHITECT_RUNNING` while Manager routes three
inline choices plus a custom-answer path through the active channel. The same
invocation receives the answer only after Manager has appended the exact
question and answer to the current ledger generation and updated the Architecture
Revision's pinned ledger reference. Replaying the same clarification is
idempotent. Reviewer receives the exact ledger generation snapshotted with the
architecture; human review displays its ordered revision history.

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

Coder and Verifier receive the same immutable task ledger plus the accepted
local module skeleton, path policy, semantic contract dependencies, assumptions,
and RepairBills. Coder treats the accepted local contract and work view as its
primary truth and uses the ledger as upstream provenance when that contract is
incomplete or contradictory. Verifier uses the accepted contract as adjudication
truth and the ledger to detect upstream omissions or conflicts. The protocol surface is already present in the
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

Coder may add durable TDD/regression cases only under the Manager-derived
`tests/<module_name>/developer` corpus and reads the Verifier corpus without
write authority. Module Verifier runs with product code and the developer
corpus read-only and writes only `tests/<module_name>/verification`. Scenario
Verifier receives a read-only
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

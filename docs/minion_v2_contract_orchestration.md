# Minion V2 Contract-Driven Orchestration

Status: active implementation as of 2026-07-14.

Minion V2 is a clean workflow cutover. V1 plans, milestones, cursors,
checkpoints, write RPCs, and workflow resume paths are not accepted by V2.
Legacy tables remain readable for diagnosis and archive tooling only.

## Business Aggregates

All business mutation enters through `ActionEnvelope` and the table-driven
transition engine in `src/pal/minion/v2/`. Reducers are pure. A committed
transition atomically performs snapshot CAS, domain-event append, action
deduplication, outbox insertion, and projection update for one aggregate.

The five aggregate types are Workflow, Architecture Revision, Execution Epoch,
DAG Node Run, and Standalone Review. Workflow phase is a query projection
derived from child aggregate state. It is not a second writable state field.

## Durable Effects

Network, LLM, process spawn, channel delivery, Git publication, and cross-
aggregate action submission are outbox effects with at-least-once delivery.
Each effect is keyed by its causative event and index. Effect receipts and
action idempotency make crash replay safe.

The manager runs long semantic effects in a bounded background task pool, so
pause/cancel/recovery effects can be claimed while an LLM worker is active.
The foreground Pal channel loop never waits for a semantic effect.

Workers hold leases with monotonically increasing fencing tokens. On startup,
an expired worker's recorded process group is terminated and reaped before the
lease is cleared. If the process group or worktree cannot be proven quiet, the
aggregate enters `TRIAGE_REQUIRED`.

## Architecture Contract

Requirements are immutable product truth. For software engineering, the
accepted code-skeleton commit plus the semantic construction and verification
topologies are architecture truth. For artifact families, the equivalent
architecture is a content-addressed manifest of semantic unit and cross-unit
contracts. Internal IDs and hashes are Manager concerns and are absent from the
LLM authoring surface.

Architect tools accept module names, exact Requirement text, paths, symbols,
interfaces, ownership, lifecycle/state/invariants, errors, compatibility, and
construction dependencies. Software Architect additionally writes only
contract-level declarations and compile wiring in an isolated worktree. It may
not write algorithms, functional implementations, milestones, test matrices,
implementation checklists, or function-level construction steps.

Research mode is explicit: `none`, `local_only`, or `external_allowed`.
`local_only` removes web capabilities from the resolved worker pack; this is
enforced by the Manager rather than prompt text. Architect research is limited
to feasibility and boundary design. Coder handles implementation-local
research from approved references and the repository.

`stateless` modules explicitly declare a stateless model. Stateful behavior
kinds must provide real lifecycle, state, and invariants.

## Schema-Bounded Authoring

LLMs do not author canonical submission JSON. Requirements, architecture,
candidate, verification, and standalone-review roles receive narrow semantic
mutation or execution tools. Terminal submit tools take no arguments. The
Manager validates the live Draft, derives hidden identities and Git deltas,
and materializes the canonical artifact before allowing the worker to exit.

Authoring Drafts are durable, lease-fenced, versioned, and operation-idempotent.
A replacement worker with the same immutable input fingerprint inherits only
semantic definitions by default. Verification and standalone-review Drafts
also inherit their recorded cases, findings, and summaries because the bound
candidate and policy are unchanged. A finding remains active across worker or
fence replacement and repeated FAIL/UNKNOWN results; a PASS for the same case
resolves it. Explicit case or finding withdrawal requires an audited reason.
Every authoring tool schema is bounded to at most 12 top-level properties and
depth four, with no arrays of objects, schema-valued `additionalProperties`, or
`oneOf`/`anyOf`. Old monolithic builder and revision-read capabilities are not
compatibility aliases.

Developer and verifier checks execute when their dedicated tool is called.
Command, environment, stdout, stderr, status, and LSP diagnostics are persisted
as immutable evidence before the Draft advances. An unchanged check can reuse
that evidence; submit never asks the Manager to reinterpret or rerun a
model-authored test-plan JSON. Artifact producers may write their product
files, but `producer_report.json` is Manager-owned and generated only by
`candidate_submit`.

## Human Governance

Architecture Markdown is mechanically compiled from the canonical manifest.
Accept/Edit/Reject cards bind workflow, revision, manifest SHA, actor, channel,
expiry, and a one-use decision token. Token consumption and the architecture
transition are one database transaction. Human Accept marks only the revision
accepted; starting execution is a separate outbox effect.

Human waivers are immutable artifacts bound to the manifest and relevant
fragment hashes. A changed fragment invalidates the waiver.

Requirements clarification uses the same actor/channel-bound, one-use decision
token mechanism and resumes at `REQUIREMENTS_QUEUED` with an immutable response
artifact.

## Execution and Verification

An accepted manifest compiles to an immutable execution epoch. A node becomes
ready only when every dependency node is `ACCEPTED`, the epoch is active, and a
slot is available. Integration is an ordinary join node depending on every
module node.

Coder receives a filtered `ModuleWorkView` containing only its contract,
requirements, architect evidence, cross-contracts, dependency outputs,
assumptions, and RepairBills. Local progress is a lease-fenced mutable journal,
not a global cursor.

Coder cannot commit. Candidate submission follows:

```text
CODING/REPAIRING -> QUIESCING -> SNAPSHOTTING -> REVIEW_QUEUED
```

Quiescing revokes the worker token, stops and reaps the process group, verifies
fencing, and holds an exclusive worktree lock. Manager then checks owned and
reference-only paths, verifies Git HEAD did not move, compares pre/post content
fingerprints, creates the candidate commit, and publishes a candidate artifact.

Verifier runs with a read-only candidate. It derives adversarial cases from
contracts, lifecycle, state, ownership, invariants, and the diff, then executes
them through dedicated fenced tools in a separate detached review worktree.
Generated scratch sources, test-worktree diff, environment, stdout, stderr, and
status are persisted before no-argument submission. Coder has no acceptance
authority; verifier has no implementation authority.

FAIL creates a RepairBill with a stable finding fingerprint and regression-test
obligation. Module defects return to the same worktree. Dependency defects
reopen the dependency and stale transitive dependents. A contract or
architecture defect is reported to the Execution Epoch; a DAG node never creates
an Architecture Revision directly.

The first such report moves the epoch to `REPLAN_COLLECTING`. Scheduling and new
worker admission stop immediately, implementation writers are stopped and made
stale, and only Reviewer/Verifier workers that were already running may finish.
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
full contract/environment fingerprint matches: module contract, relevant
requirements/evidence, global constraints, owned area, dependency set,
dependency interfaces and outputs, integration subset, environment policy, and
epoch baseline. Reused commits are imported by the manager; partial matches rerun.

## Sidecar-Owned Profile Catalog

The Minion sidecar exclusively owns profile and family catalog lifecycle.
Package templates are immutable builtins and are loaded directly for every
registry projection; the Wizard and Pal process do not copy, parse, refresh, or
modify them. Runtime state contains only explicit JSON overrides under
`data/minion/catalog/`.

On attach, the sidecar archives legacy `plugins/minion/profiles` and
`plugins/minion/families` TOML seeds before constructing workers. Explicit
legacy custom definitions are converted to sidecar-owned overrides; managed
builtin seeds are removed from effective precedence so they cannot mask an
upgraded package template.

Catalog reads, semantic merge patches, resets, and refreshes are manager IPC
operations. Writes are schema-validated, atomically published, audited, and
optionally guarded by catalog generation CAS. Existing workflows retain their
immutable `FamilyBindingArtifact` and profile hashes; catalog changes affect
only subsequently created workflows.

## Public Surface

Pal sees eight workflow capabilities:

- `op_minion_start_workflow`
- `op_minion_submit_artifact`
- `intro_minion_task_search`
- `intro_minion_workflow_status`
- `op_minion_resume_workflow`
- `op_minion_submit_human_decision`
- `op_minion_control_workflow`
- `op_minion_archive_workflow`

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

V2 tables use the `minion_v2_` prefix in Minion's SQLite database. Artifact
bytes live under `data/minion/artifacts/sha256/`; SQLite stores metadata and
reference edges. Publication writes a same-filesystem temporary file, fsyncs,
atomically renames, fsyncs the parent, and only then records durable metadata.
The address includes artifact type, schema version, media type, and bytes, so
identical JSON used for different semantic artifact types cannot overwrite
metadata.

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

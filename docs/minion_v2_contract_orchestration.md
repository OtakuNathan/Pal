# Minion V2 Contract-Driven Orchestration

Status: active implementation as of 2026-07-10.

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

The canonical architecture is a content-addressed manifest over immutable
requirements, evidence, constraints, decisions, module contracts, cross-module
contracts, topology, integration, assumptions, and risks.

Requirements Analyst, Researcher, Contract Planner, and Architecture Reviewer
have separate profiles and capability surfaces. Research mode is explicit:
`none`, `local_only`, or `external_allowed`. Mode `none` may carry only
already-approved input evidence. `local_only` removes web capabilities from the
resolved worker pack; this is enforced by the manager rather than prompt text.

Module contracts contain responsibility, owned/reference-only paths,
interfaces, ownership, lifecycle/state/invariants, errors, compatibility,
dependencies, requirement/evidence references, verification obligations, and
a structured complexity budget. They cannot contain milestones, test matrices,
implementation checklists, or function-level steps.

`stateless` modules explicitly declare a stateless model. Stateful behavior
kinds must provide real lifecycle, state, and invariants.

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
contracts, lifecycle, state, ownership, invariants, and the diff. Manager reruns
declared commands in a separate detached review worktree and persists commands,
generated scratch sources, test-worktree diff, environment, stdout, stderr, and
status. Coder has no acceptance authority; verifier has no implementation authority.

FAIL creates a RepairBill with a stable finding fingerprint and regression-test
obligation. Module defects return to the same worktree. Dependency defects
reopen the dependency and stale transitive dependents. Contract or architecture
defects freeze the epoch and create a new architecture revision. Three identical
findings with no candidate-tree change enter triage.

UNKNOWN is nonblocking only with an allowed architecture policy, a complete
assumption reference, and a valid human waiver for hard/security/permission/
public-API semantics.

After architecture replan, accepted module candidates are reused only when the
full contract/environment fingerprint matches: module contract, relevant
requirements/evidence, global constraints, owned area, dependency set,
dependency interfaces and outputs, integration subset, environment policy, and
epoch baseline. Reused commits are imported by the manager; partial matches rerun.

## Public Surface

Pal sees exactly seven Minion V2 capabilities:

- `op_minion_start_workflow`
- `op_minion_submit_artifact`
- `intro_minion_workflow_status`
- `op_minion_resume_workflow`
- `op_minion_submit_human_decision`
- `op_minion_control_workflow`
- `op_minion_archive_workflow`

Lease, outbox, scheduler, spawn, tick, and recovery operations are manager-only.

## Persistence

V2 tables use the `minion_v2_` prefix in Minion's SQLite database. Artifact
bytes live under `data/minion/v2/artifacts/sha256/`; SQLite stores metadata and
reference edges. Publication writes a same-filesystem temporary file, fsyncs,
atomically renames, fsyncs the parent, and only then records durable metadata.
The address includes artifact type, schema version, media type, and bytes, so
identical JSON used for different semantic artifact types cannot overwrite
metadata.

The status projection always reports one current phase, active aggregate and
worker, blocker, legal next actions, user wait, cumulative metrics, last event,
and liveness. Deliberate `PAUSED`, human wait, and operator/triage wait are
valid quiet states; an otherwise orphaned workflow is moved to triage.

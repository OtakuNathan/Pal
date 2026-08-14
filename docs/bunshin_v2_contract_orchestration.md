# Bunshin Contract-Driven Orchestration

Status: active clean cutover as of 2026-07-30.

Bunshin is a contract graph executor. A Family selects the problem domain and
pins one Architect, Reviewer, Implementation, and Verifier binding. The graph
shape is shared across families; the module definition schema and role
participants are data.

## Truth Sources

Manager owns an immutable `TaskLedgerArtifact`, materialized to roles as
read-only `task.yaml`. Its original request is preserved verbatim. Clarification
answers are appended mechanically as ordered revisions; newer revisions win
only where meanings conflict.

Architect is the only role that consumes the complete task as its primary
input. Coder and Verifier receive a Manager-derived view of one module. Their
authority order is:

1. current code and normative adjacent comments;
2. the accepted module contract;
3. the Manager-derived module view;
4. the current WorkItem checklist;
5. `task.yaml` as a final fallback.

Git diff, log, and blame are the only truth about code changes. A checklist is
an execution cursor, never contract truth or evidence.

## Family And Role Protocol

`FamilyBindingArtifact` schema v6 pins:

- the Family architecture specialization, compiled Draft 2020-12 schema,
  rendered Architect form, and one immutable generation hash;
- role profiles and their versions;
- each role participant (`profile` or `null`);
- one Family-selected execution adapter strategy;
- family policies.

Role TOML owns the playbook: identity, behavior, ordered truth sources,
checklist policy, and terminal submission tool. Profile prose teaches the
method; the Manager supplies bounded inputs; WorkItems drive execution.

A `null` participant closes a graph node without spawning an LLM. Architect and
Reviewer remain profile-backed; Implementation and Verifier must either both
use profiles or both use explicit `null` participants. Lifestyle plans can
therefore use the same Architect/Reviewer/Implementation/Verifier graph while a
human performs execution. Other families may map modules to days, slides,
sheets, chapters, or any schema-defined semantic unit.

Role participation and execution harness are independent contracts. A role
profile defines domain identity and method. An immutable harness registry
selects the process that executes that role. Pal is the universal fallback;
an attached, healthy higher-priority harness may specialize a role without
changing the Family binding. Each attempt captures one registry generation, so
detach affects only later attempts. Two failed attempts on a preferred external
harness fall back to Pal for that assignment.

## Contract Protocol

The only architecture handoff is `ContractArtifact`. Manager combines its base
architecture template with the Task-bound Family specialization and pre-seeds
one rendered `architect.yaml`. Its fixed envelope contains:

- `context`;
- requirement claims, owners, and contract paths;
- semantic modules;
- dependency consumption and handoff semantics;
- end-to-end scenarios.

Each module has one responsibility, execution kind, provided outputs,
dependencies, and a family-specific `definition`. The pinned Draft 2020-12
schema defines that `definition`. Raw schema and compiler inputs remain
Manager-only: the role sees only the rendered instance-shaped form. On submit,
the Manager validates that instance against the exact FamilyBinding generation.
The common validator rejects duplicate YAML keys, aliases, merges, unknown
fields, missing owners, unknown dependencies, cycles, unconsumed requirements,
and broken scenario references.

Architect first reads the task and designs the complete module graph. In the
software family it then writes declaration-level code and normative comments.
Only after the design is settled does it encode `architect.yaml`, reconcile both
projections, complete its checklist, and call `contract_submit`.

Reviewer receives the same task ledger plus the complete immutable contract.
It audits breadth-first, traces success and material failure paths through the
contract graph, records every independent defect with `add_finding`, completes
the checklist, and calls `review_submit`. It never fail-firsts and never emits
Markdown as a machine contract. PASS is mechanically impossible while a
blocking finding exists; p2 is blocking unless explicitly advisory.

Markdown shown to a human is a deterministic projection of `ContractArtifact`.
It is not another truth source.

## WorkItems

Checklist tasks and findings are one Manager-owned WorkItem ledger:

- `task` items are the role's playbook cursor;
- `finding` items are structured reviewer/verifier defects;
- Manager-routed repair findings become required checklist work.

The LLM supplies semantic keys and content, never durable IDs. Manager assigns
identity, deduplicates semantic updates, fences mutations to the active role
assignment, and validates checklist closure at submit. A replacement physical
attempt with the same logical assignment and input fingerprint inherits the
ledger.

## Execution Adapters

`ContractArtifact` compiles through one of two internal adapters:

- `software_git.v2` projects the software definition into the private
  Git/skeleton engine, canonical module worktrees, developer/verifier corpora,
  and a system-delivery node;
- `artifact_bundle.v2` creates content-addressed module workspaces for data
  families.

These projections are compiler inputs, not public architecture contracts.
ExecutionCompiler and module-view construction reject every non-Contract
artifact.

One semantic module owns one canonical worktree and one long-lived
Coder/Verifier logical pair. Candidate and repair assignments reuse that pair.
Replan preserves the worktree, sessions, tests, and bounded continuation when
the module name and responsibility remain stable. Added modules allocate new
resources; deleted or responsibility-replaced modules retire them only after
their native process group exits cleanly.

Coder writes product code plus `tests/<module>/developer`. Verifier shares the
same baseline, keeps product/developer paths read-only, and owns
`tests/<module>/verifier`. It first replays durable regressions, then reviews
the current Git delta and adds new adversarial coverage. Findings route repairs
back through the same WorkItem protocol.

One workflow-level System Verifier performs system and delivery tests against
the accepted candidate union. It uses real entrypoints, including PTY-driven
interactive surfaces when applicable. A scenario is an identity of this one
role, not another subagent.

## Durable Lifecycle

Aggregate state remains first-class. Business mutation uses table-driven
`ActionEnvelope` transitions with snapshot CAS, event append, action
deduplication, and outbox insertion in one transaction.

A logical role session owns continuation, file-read snapshots, and pager
handles. One assignment may be open per session:

```text
QUEUED -> CLAIMED -> RUNNING -> RESULT_RECORDED -> SETTLED
                    +---> RETRY_QUEUED -> CLAIMED
```

Physical attempts own a subprocess group, bwrap sandbox, lease, fencing token,
and LSP process. They exit and reap through one RAII-style path. Endpoint retry
reuses the assignment and continuation. Late IPC without a matching active
protocol call is discarded.

Module acceptance suspends its logical Coder/Verifier sessions. They close only
when the module is deleted/replaced, the workflow terminates, or an operator
explicitly resets that identity.

## Human Governance

Architecture Accept/Edit/Reject is bound to workflow, revision, Contract SHA,
actor, channel, and a single-use durable decision token. Inline controls have
no wall-clock expiry; the logical coroutine can archive and resume.

Human Edit corrects task meaning. Manager appends the exact exchange to
`task.yaml`; Architect revises the existing complete Contract generation.
Reviewer regresses the prior finding and changed semantic neighborhood before
another human review.

## Cutover

There are no compatibility aliases or public fallbacks for:

- `architecture.yaml`;
- Contract/Skeleton Builder tools;
- `ArchitectureContractArtifact`;
- `ArchitectureSkeletonArtifact`;
- separate reviewer surface/conclusion manifests;
- LLM-authored checklist/finding IDs.

Old runtime schemas are archived rather than migrated in place. New work must
start from a FamilyBinding v5 task and produces only ContractArtifact
architecture generations.

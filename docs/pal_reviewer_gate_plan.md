# Pal Reviewer Gate Hardening Plan

Status: historical hardening plan. The current implementation sync point is
`pal_minion_v1.md#gate-loop`. The implemented system is profile-driven:
profiles declare `[gate_policy] gates = [...]`, runtime expands those names into
`GateSpec` values, and the review orchestrator schedules gates after milestone
result events.

This plan turns the reviewer from an optional after-the-fact reviewer into a required gate in Pal's software-engineering minion workflow.

It is intentionally strict. Pal V1 has not been publicly released, so the workflow should be cleaned up now instead of preserving weak compatibility.

## Target Outcome

The manager, not the coder, decides whether work advances.

The desired software-engineering loop:

```text
planner produces plan_ref
  -> reviewer verifies plan
  -> Pal/user accepts reviewed plan
  -> manager dispatches coder milestone
  -> coder claims checkpoint
  -> reviewer verifies checkpoint
  -> manager either advances, sends repair, or blocks
```

The reviewer must join the coder loop. It should not be a final summary writer.

## Hard Invariants

- A planner plan is not dispatchable until a reviewer has verified it or a human override is explicitly recorded.
- A coder checkpoint is only a claim, not milestone closure.
- A milestone closes only after manager records a passing reviewer/verifier gate.
- A failed review sends a bounded repair turn to the same coder runner whenever possible.
- A partial review cannot silently pass. It either blocks or requires explicit human override.
- Review outputs must be structured artifacts or structured capability results, not chat prose.
- The manager records every gate decision in the ledger.
- Shell must not be accepted as an invisible editor. Workspace mutations need structured evidence.

## State Model

### Plan States

```text
plan_created
plan_review_pending
plan_review_passed
plan_review_failed
plan_accepted
plan_revised
plan_override_accepted
```

`minion_accept_plan` should require a passing plan-review gate unless `human_override` is explicitly provided with a reason.

### Milestone States

```text
milestone_assigned
coder_running
coder_checkpoint_claimed
checkpoint_review_pending
checkpoint_review_passed
checkpoint_review_failed
repair_assigned
milestone_closed
milestone_blocked
```

The existing "checkpoint completed advances cursor" behavior should be changed. The coder emits a claimed checkpoint; the manager writes the final milestone closure after reviewer pass.

## Review Gate Result

Add one typed review gate result shape used for both plan review and checkpoint review.

```json
{
  "gate_kind": "plan_acceptance | checkpoint_verification | repair_verification",
  "target": {
    "plan_ref": {},
    "checkpoint_id": "",
    "work_order_id": "",
    "run_id": "",
    "module_id": "",
    "milestone_id": "",
    "milestone_index": 0,
    "commit_sha": ""
  },
  "verdict": "pass | fail | partial",
  "summary": "",
  "findings": [],
  "required_fixes": [],
  "evidence": [],
  "commands_run": [],
  "api_evidence": [],
  "residual_risk": [],
  "report_artifact_ref": {}
}
```

Rules:

- `verdict=pass` requires concrete evidence.
- `verdict=fail` requires actionable findings or a clear blocker.
- `verdict=partial` requires explicit unavailable evidence or environment limitation.
- `commands_run[]` entries should include command, cwd, exit code, and observed output summary.
- `api_evidence[]` can use source inspection, docs, LSP evidence, build/test evidence, or explicit "not verified" findings.
- The gate target must bind to the exact plan revision or checkpoint commit being reviewed.

## Capability Surface

The implemented reviewer surface has one generic plan-review submitter plus a
checkpoint-specific wrapper:

```text
op_minion_review_gate_submit
op_minion_review_checkpoint
```

Inputs:

- `gate_kind`
- `target`
- `verdict`
- `summary`
- `findings`
- `required_fixes`
- `evidence`
- `commands_run`
- `api_evidence`
- `report_artifact_ref`

Policy:

- Expose it only to reviewer/verifier profiles.
- The repository validates target existence and hash/commit binding.
- The manager consumes the gate result; the runner does not self-advance.
- Use `op_minion_review_gate_submit` for `plan_acceptance`.
- Use `op_minion_review_checkpoint` for `checkpoint_verification` and `repair_verification`; Pal binds the checkpoint target and tool evidence.

Plan acceptance should then change from:

```text
accept_plan(plan_ref)
```

to:

```text
accept_plan(plan_ref, review_gate_ref)
```

with optional explicit override:

```text
accept_plan(plan_ref, human_override={reason, actor})
```

Overrides must be visible in the acceptance marker and ledger.

## Manager Loop

For each coder milestone:

1. Manager sends exactly one milestone turn to the coder runner.
2. Coder implements and calls `checkpoint_commit`.
3. Runner emits `coder_checkpoint_claimed`.
4. Manager records the claim and does not advance the milestone cursor yet.
5. Manager spawns or schedules a reviewer run with a scoped review work order.
6. Reviewer inspects plan, diff, checkpoint commit, tests, and evidence.
7. Reviewer submits the gate. Plan reviewers use `op_minion_review_gate_submit`; checkpoint and repair reviewers use `op_minion_review_checkpoint`.
8. Manager handles verdict:
   - `pass`: write milestone closure, advance cursor, send next milestone to same coder runner if available.
   - `fail`: send same coder runner a repair turn with `review_gate_ref` and required fixes.
   - `partial`: block and ask Pal/user unless policy allows retry.

The coder runner should remain alive through review where practical. This preserves useful local context while still preventing self-approval.

## Reviewer Work Order

Reviewer should receive only what it needs:

- plan_ref and exact plan revision
- target module/milestone
- checkpoint commit SHA and base SHA
- changed file list/diff summary
- coder milestone report
- test evidence claimed by coder
- relevant source paths
- allowed read/search/LSP/test capabilities
- artifact output directory

Reviewer should not receive:

- future milestones unrelated to the review
- write access to the coder workspace
- minion control capabilities
- memory mutation capabilities

Reviewer may write temporary probes under `/tmp`, `$TMPDIR`, or an isolated verifier workspace.

## Reviewer Output Contract

Reviewer output must be short in chat and structured in artifact/capability result.

Required artifact for non-trivial software work:

```text
review_report.json
```

Optional human-readable companion:

```text
review_report.md
```

The JSON report must contain the same gate fields submitted through the reviewer gate capability.

## Coder Repair Turn

On failed review, the manager sends the same coder runner a repair turn:

```json
{
  "turn_kind": "repair",
  "same_milestone": true,
  "review_gate_ref": {},
  "required_fixes": [],
  "findings": [],
  "instructions": "Address the review findings, update tests, and create a new checkpoint claim."
}
```

The coder must not proceed to the next milestone until the current milestone passes review.

Repair attempts should be bounded:

- builtin `checkpoint_quality` default max automatic repair attempts: 5
- after that, block and ask Pal/user

This prevents infinite "review -> repair -> review" loops.

## Plan Review Gate

Plan reviewer checks:

- plan artifact is dispatchable and topology is valid
- modules and milestones are coherent
- first milestone establishes architecture/contracts where needed
- join/integration milestone exists
- APIs and external assumptions are verified or explicitly marked unknown
- test strategy matches implementation risk
- work can be executed one milestone at a time

Plan reviewer verdict:

- `pass`: plan can be accepted
- `fail`: plan must be revised
- `partial`: plan needs human decision or missing external truth source

`minion_revise_plan` remains the mechanism for updating the same plan. The new reviewer gate decides whether the revised plan is acceptable.

## Checkpoint Review Gate

Checkpoint reviewer checks:

- implementation matches the current milestone only
- changed files match owned area and expected contracts
- tests were run and are relevant
- claimed API usage exists in source/docs/LSP/build evidence
- no unexplained shell mutation exists
- checkpoint commit includes the right files and excludes generated noise/secrets
- code quality risks are reported with file/line evidence

Reviewer pass should not mean "perfect"; it means "sufficient evidence for this milestone to advance, with residual risks recorded."

## Shell Mutation Audit

Do not implement a full shell parser.

Instead:

- before shell execution, record workspace snapshot or git status
- after shell execution, inspect mutation
- if workspace changed without structured file edit/write/checkpoint evidence, record `shell_mutation_violation`
- a checkpoint with unresolved violation cannot pass review

Start with Git-backed coder workspaces because they have clear diff/status semantics.

## LSP Integration

LSP is not required for the first reviewer gate slice, but the gate schema should already allow `api_evidence[]`.

When the first-party LSP provider exists, reviewer can include evidence from:

```text
lsp_definition
lsp_hover
lsp_implementation
lsp_references
lsp_prepare_call_hierarchy
lsp_incoming_calls
lsp_outgoing_calls
lsp_diagnostics
lsp_doctor
```

LSP evidence is useful but not absolute. For high-risk API claims, pair it with source/docs/build/test evidence.

## Implementation Slices

### Slice 1: Structured Review Gate

- Add `ReviewGateResult` validation model.
- Add repository storage/ledger event for review gates.
- Add review gate submit surfaces.
- Add tests for pass/fail/partial validation.

### Slice 2: Plan Acceptance Gate

- Update `minion_accept_plan` to require a passing plan-review gate.
- Store review gate ref in acceptance marker.
- Add explicit human override path with reason.
- Add tests for missing review, failed review, stale review, and override.

### Slice 3: Coder Checkpoint Claim Semantics

- Change runner/manager semantics so coder checkpoint is claimed, not closed.
- Prevent current milestone cursor from advancing until reviewer pass.
- Add ledger events for claim, review pending, review pass/fail, milestone closed.
- Add tests that a claimed checkpoint alone does not advance the current milestone.

### Slice 4: Reviewer In The Coder Loop

- Manager spawns reviewer after coder checkpoint claim.
- Reviewer gets scoped reviewer work order.
- On pass, manager sends next milestone to the same coder runner if alive.
- On fail, manager sends repair turn to the same coder runner.
- Add tests for pass continuation and fail repair.

### Slice 5: Shell Mutation Audit

- Track shell execution workspace mutation for Git-backed coder workspaces.
- Record unauthorized mutation violations.
- Make reviewer gate fail or block on unresolved violations.
- Add tests for shell-created file without edit evidence.

### Slice 6: Dogfood

Run a small multi-milestone task:

1. Planner produces plan.
2. Reviewer passes or fails plan.
3. Accept reviewed plan.
4. Coder completes milestone 1 and creates checkpoint claim.
5. Reviewer fails once with a concrete finding.
6. Same coder runner repairs milestone 1.
7. Reviewer passes.
8. Manager advances to milestone 2.

Success criteria:

- no milestone advances from coder text alone
- plan acceptance records review gate
- checkpoint closure records review gate
- repair turn reuses same coder run when possible
- ledger can answer "why did this milestone close?"

## Tests To Add

- `test_accept_plan_requires_passed_review_gate`
- `test_accept_plan_rejects_failed_review_gate`
- `test_accept_plan_records_human_override`
- `test_checkpoint_claim_does_not_advance_cursor`
- `test_manager_pass_review_advances_same_runner`
- `test_manager_failed_review_sends_repair_turn`
- `test_review_gate_rejects_stale_plan_revision`
- `test_review_gate_rejects_wrong_checkpoint_commit`
- `test_shell_mutation_violation_blocks_checkpoint_review`
- `test_reviewer_profile_cannot_write_coder_workspace`

## Non-Goals

- Do not build full LSP in the first gate slice.
- Do not build a complete shell parser.
- Do not introduce DAG scheduling yet.
- Do not make reviewer auto-fix code directly in the coder workspace.
- Do not make plan review depend on natural-language chat summaries.

## Open Decisions

- Whether plan review is always required or only required for software-engineering profiles.
- Whether a human override should be allowed for checkpoint review, and who can trigger it.
- Whether reviewer should be a separate profile or a stricter verifier subtype for some gates.
- Whether checkpoint claim and milestone closure should use one table with statuses or separate `checkpoint_claims` and `review_gates` tables.

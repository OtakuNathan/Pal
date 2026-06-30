# Minion Repair Bill Replay

This document records the planned repair-propagation model for minion DAG work
orders.

## Problem

The current module DAG assumes the architect produced a structurally correct
plan and that each module can be validated locally. In practice, downstream
integration or final verification can discover an upstream defect that was not
covered by the original module acceptance criteria.

The repair path should not rely on the downstream module silently patching an
upstream implementation, copying private files, or asking the manager to infer a
root cause from logs. The downstream module should collect a structured defect
bill, stop, and let the manager replay the affected DAG region with amended
constraints.

## Core Model

A repair bill is isomorphic to the existing module plan shape. It uses
`module_id` as the merge key and adds only the missing constraints, evidence,
and counterexamples for modules that need repair.

The manager remains mechanical:

- it validates that referenced `module_id` values exist in the current plan
- it merges bill entries into a per-module repair overlay
- it marks targeted modules as needing repair
- it marks downstream dependents as stale
- it schedules replay through the existing DAG, slot, workspace, and gate logic

The manager does not use an LLM to decide which module to spawn next. It consumes
the bill as extra plan constraints and reuses the existing scheduler.

## Defect Kinds

`integration_defect`

The problem is local to the reporting module's own integration code, tests, or
join glue. The module's reviewer/repair loop should handle it locally. A manager
replay bill is not needed.

`module_defect`

The target module implementation violates its declared public contract or misses
an acceptance criterion that should have existed. The bill appends new acceptance
criteria, negative cases, and evidence to that module.

`contract_defect`

The public contract itself is incomplete, ambiguous, or wrong. The contract-owning
module receives amended contract criteria, and all consumers downstream of that
contract become stale.

`architecture_defect`

The module boundary, dependency graph, or ownership model is wrong. This is not a
normal replay. The current DAG should pause, and Pal should request a new
architect pass or a new DAG epoch.

`triage_required`

The reporting module found a real problem but cannot map it to a specific
existing module. Pal should surface the bill for human or architect triage rather
than guessing.

## Bill Shape

The durable bill can be stored as JSON, while summaries may be rendered in
Markdown for users.

```json
{
  "bill_id": "bill_final_verification_001",
  "parent_work_order_id": "wo_parent",
  "source_module_id": "final_verification",
  "status": "submitted",
  "module_patches": {
    "vm": {
      "defect_kind": "module_defect",
      "summary": "Anchors encoded as sentinel save slots were dropped by the VM.",
      "additional_acceptance_criteria": [
        "VM preserves zero-width anchor opcodes during thread advancement."
      ],
      "negative_cases": [
        {
          "name": "empty string anchors",
          "input": "pattern='^$', text=''",
          "expected": "match succeeds"
        }
      ],
      "evidence": [
        {
          "source": "final_verification",
          "artifact": "reports/final_verification.md",
          "detail": "The integrated test failed before vm.cpp handled negative save slots."
        }
      ]
    },
    "contracts": {
      "defect_kind": "contract_defect",
      "summary": "Anchor representation was not declared as part of the compiler/VM handoff.",
      "additional_acceptance_criteria": [
        "Compiler and VM contract explicitly defines anchor opcode representation."
      ],
      "evidence": [
        {
          "source": "final_verification",
          "artifact": "reports/final_verification.md"
        }
      ]
    }
  }
}
```

## Merge Rules

The accepted architect artifact remains immutable for audit. Repair bills are
merged into a repair overlay keyed by `parent_work_order_id` and `module_id`.
When the scheduler materializes a module prompt, it reads the original module
definition plus all active overlay entries for that module.

For `module_defect`:

- append `additional_acceptance_criteria` to the target module's effective AC
- append `negative_cases` to the target module's required test evidence
- append `evidence` to the repair prompt and reviewer context
- mark the target module `needs_repair`
- mark all downstream modules `stale`

For `contract_defect`:

- apply the same overlay behavior to the contract-owning module
- mark all direct and transitive consumers stale
- require downstream modules to rerun against the amended public contract

For `architecture_defect`:

- do not replay automatically
- pause the parent work order
- preserve the bill as architect input
- create a new DAG epoch or replacement plan after review

For unknown modules or invalid bill shape:

- reject the bill as `triage_required`
- keep the current DAG state unchanged
- surface the bill summary to Pal/user

## Replay State

Replay should use explicit module states rather than overloading completed
checkpoints:

- `completed`: module output is valid for the current DAG epoch and repair
  overlay version
- `needs_repair`: module must rerun with amended constraints
- `stale`: module previously completed but depends on a module that must rerun
- `running`: module currently owns a scheduler slot
- `blocked`: module cannot continue without outside input

When a repaired module passes its gate, the manager recomputes ready modules from
the DAG exactly as it does for first-run scheduling. Stale downstream modules
become ready only after their dependencies are completed under the current repair
overlay version.

## Runner Responsibilities

A downstream module or final verification module should produce a repair bill
only after local repair is exhausted or the evidence clearly points upstream.

The reporting module should:

- keep solving `integration_defect` locally
- collect the smallest reproducible counterexample
- identify the most specific target `module_id` it can defend
- distinguish `module_defect` from `contract_defect` when possible
- stop after submitting the bill instead of modifying upstream-owned files

Reviewer gates should reject bills that are unsupported by evidence or that
attempt to claim ownership of unrelated modules.

## Implementation Notes

The first implementation can be deliberately small:

- persist bills under minion-owned tasking state or
  `data/minion/repair_bills/{parent_work_order_id}/`
- add a repair overlay table or artifact keyed by
  `(parent_work_order_id, module_id)`
- extend scheduler state reconstruction to treat overlay-version mismatch as
  `stale`
- add a manager operation that accepts a validated bill and triggers scheduling
- include active overlay entries in coder and reviewer context packs

This keeps repair propagation as "DAG replay with amended constraints" instead
of a second planning system.

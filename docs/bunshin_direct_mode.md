# Bunshin direct mode

Use `start_bunshin_workflow` with `execution_mode: "direct"` for a clear repository
change, a plan already settled with Pal, or a delegated investigation. Pal chooses
the mode explicitly and follows the user's preference. Omitting the field retains
`planned`, including architecture design, review, and approval.

Direct mode currently accepts `operation: "new_requirement"` in the
`software_engineering` family. Both `existing_repo` and `new_project` workspaces
are supported. Existing tasks keep their pinned profiles; tasks pinned before
direct-mode support must be recreated with current profiles. A normal direct
invocation looks like:

```json
{
  "profile": "software_engineering.v2_coder",
  "execution_mode": "direct",
  "operation": "new_requirement",
  "goal": "Investigate the reconnect failure",
  "workspace": {
    "kind": "existing_repo",
    "repo_path": "/absolute/path/to/project",
    "primary_language": "python"
  },
  "task_spec": {
    "authoritative_text": "<complete original request and confirmed decisions, verbatim>",
    "deliverable_paths": ["reports/reconnect.md"]
  }
}
```

For an existing Task use `task` instead of supplying profile/workspace. Existing
`task_spec_file` and approved skill inputs retain their normal semantics.
Repository-relative file references use the existing immutable input binder;
absolute external file references are captured once and supplied read-only to both
roles. Include repository directories through the workspace, rather than as
external directory references. A short goal does not replace the complete task. `deliverable_paths`
is optional; it names regular repository-relative files that must exist in the
verified result. It cannot name control state, immutable inputs, or worker report
protocol files.

## Execution and authority

The Manager captures the source snapshot and immutable task ledger in a
`DirectExecutionArtifact`, then compiles one `repository` node through the shared
software Git adapter. There is no Architect session, Architecture Reviewer,
PlanCycle, authored module decomposition, or synthetic architecture commit.
Coder and Verifier use the normal candidate, repair, verification, and sink
publication lifecycle. Both see the same original task, exact revisions, bound
materials, and work view. Direct role fragments retain the shared engineering
discipline but use the task as primary authority.

The node owns the entire repository, including existing tests, build files, and
new top-level paths, within the task's scope. VCS and Manager state, immutable
inputs, and verifier-owned regression tests retain their normal protection. The
source repository stays outside the worker sandbox; the worker edits its isolated
worktree. Verifier remains independent and writes only its owned corpus.

## Clarification and recovery

Coder calls `report_task_blocker` for contradictory or underspecified requirements.
Verifier's requirements/contract/architecture findings also return to Pal through
triage. They never escalate into an Architect run. Ordinary implementation defects
continue through Coder repair and independent verification.

Pal resolves a task blocker with `resolve_bunshin_triage`. Manager records the exact
answer as an append-only task revision, advances the binding and graph generation,
invalidates the old candidate verdict, and requeues Coder. Existing worktree edits,
logical sessions, findings, and regression files remain available. The same
updated ledger is supplied to both roles.

Normal pause, resume, cancellation, and process recovery use the existing engine.
Execution restart preserves direct mode and the original source snapshot, task
ledger, and captured input bytes. It creates a replacement workflow with fresh
execution worktrees rather than reusing candidates. No architecture review is
introduced on restart. No database migration is required.

## Delivery and verification

Requested report files are independently verified and extracted from the accepted
Git commit, never from a later dirty worktree. Durable content-addressed files are
attached through the existing completion event and remain available after sandbox
cleanup. With code changes, the verified whole-tree patch is attached as well.
Without `deliverable_paths`, the existing patch delivery remains unchanged.

Pure reports in `.md`, `.txt`, `.rst`, `.log`, or `.pdf` format can replace the patch
attachment. They retain independent evidence and focused checks but do not require
an invented public entrypoint, warning build, or LSP check. Source files named as
attachments still require code verification and patch delivery. Public-surface
checks remain available when relevant to a direct task. The new version-4 receipt
contains files and an optional version-3 patch receipt; version-3 deliveries remain
readable.

## Activation

This change updates `pal.bunshin` and its bundled profiles. Reload the existing
Bunshin plugin when Pal is idle, following `pal.self.maintenance`; attaching an
already-attached plugin does not reload it. Existing Task profile bindings remain
immutable. Create a new Task to pick up the direct role fragments. No running
instance is reloaded by changing these source files or running the tests.

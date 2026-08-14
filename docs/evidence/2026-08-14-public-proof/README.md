# 2026-08-14 Pal Public Proof Dogfood

This directory preserves two metadata-only captures from one real workflow:

- [`recovery-checkpoint.md`](recovery-checkpoint.md) was captured after the
  full Pal service was restarted during Architecture Reviewer execution. It
  records the lost first attempt, the replacement attempt with a newer fencing
  token, the unchanged source commit, and a clean tracked worktree.
- [`proof.md`](proof.md) is the terminal capture. It preserves the complete
  event chain, artifacts, role attempts, triage, and operator cancellation.

The companion JSON files use the `pal.public-proof.v1` schema and contain the
redacted machine-readable records behind each Markdown view.

## What passed

- A request sent through Pal's socket channel became a durable Task and
  Workflow.
- Architect produced a content-addressed contract; Architecture Reviewer
  reviewed it; the human decision was submitted through Pal's control plane.
- Restarting the complete Pal service while Architecture Reviewer was active
  ended attempt 1. The new Manager recovered the same durable assignment as
  attempt 2 with fencing token 2.
- Both captures have contiguous aggregate event versions and valid typed
  artifact hashes.
- No tracked file in the source repository changed before the recovery
  checkpoint.

## What failed

This run was intentionally preserved rather than polished into a false success.
The Task named three repository paths as inputs to a `general` family artifact
workflow. Those paths were not materialized as bound references inside the
node's sandbox. `source_review` correctly produced a blocker report instead of
inventing the missing document contents.

The Verifier then attempted Git evidence collection in that artifact workspace,
which was not a usable Git worktree, and the node entered `TRIAGE_REQUIRED`.
The operator selected cancellation through Pal's normal control surface. The
Workflow reached `CANCELLED`; it was not retried, archived, or deleted.

The producer also spent 33 completed turns and recorded 1,107,343 aggregate
input tokens before closing the missing-input blocker. That is evidence of an
early-failure/tool-friction problem, not a performance result worth celebrating.
The public happy path should bind its inputs before dispatch and fail much
earlier when a required reference is absent.

This means the capture proves persistence, recovery, fencing, fail-closed input
handling, triage, and auditability. It does **not** prove successful end-to-end
report delivery. A happy-path recording must bind the source documents as
explicit immutable inputs or use a workflow family whose workspace contract
includes the repository.

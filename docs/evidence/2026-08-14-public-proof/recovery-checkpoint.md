# Pal Public Proof

Evidence schema: `pal.public-proof.v1`  
Captured: `2026-08-14T07:34:53.683818+00:00`

This report is generated from Pal's read-only Bunshin event store. It contains
state and content hashes, not prompts, secrets, private artifact contents, or
provider credentials.

## Run

- Task: `task_38807641e2a94053badfa349690548e2` — Pal public proof demo 报告（general 技术写作/评审 dogfood）
- Workflow: `wf_dfe41605761c45928e7fb6eceef02c53`
- Family/profile: `general` / `generic`
- State: `ACTIVE` / `human_review`
- Liveness: `human_wait`

## Mechanical checks

- PASS — `task_bound`
- PASS — `event_chains_contiguous`
- PASS — `referenced_artifacts_present`
- PASS — `referenced_artifact_hashes_valid`
- PASS — `recovery_observed`
- PASS — `recovery_fencing_monotonic`
- PASS — `repository_head_matches`
- PASS — `repository_tracked_tree_clean`

## Role attempts

| assignment | attempt | fence | status | error |
| --- | --- | --- | --- | --- |
| asg_bba6768b… | 1 | 1 | completed | — |
| asg_19e22022… | 1 | 1 | lost | worker_process_failed |
| asg_19e22022… | 2 | 2 | completed | — |

## Aggregate state

| type | id | version | state | last action |
| --- | --- | --- | --- | --- |
| workflow | wf_dfe41605… | 3 | ACTIVE | LINK_ARCHITECTURE_REVISION |
| architecture_revision | arch_2c2d36b3… | 7 | HUMAN_REVIEW | HUMAN_REVIEW_PUBLISHED |

## Evidence inventory

- 10 append-only domain events
- 2 durable role invocations
- 10 referenced content-addressed artifacts verified
- 3 durable delivery records

The companion JSON file contains the complete redacted timeline and hashes.
Artifact bodies are intentionally excluded.

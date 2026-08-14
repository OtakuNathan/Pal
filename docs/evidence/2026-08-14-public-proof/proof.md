# Pal Public Proof

Evidence schema: `pal.public-proof.v1`  
Captured: `2026-08-14T07:45:32.157736+00:00`

This report is generated from Pal's read-only Bunshin event store. It contains
state and content hashes, not prompts, secrets, private artifact contents, or
provider credentials.

## Run

- Task: `task_38807641e2a94053badfa349690548e2` — Pal public proof demo 报告（general 技术写作/评审 dogfood）
- Workflow: `wf_dfe41605761c45928e7fb6eceef02c53`
- Family/profile: `general` / `generic`
- State: `CANCELLED` / `cancelled`
- Liveness: `terminal`

## Mechanical checks

- PASS — `task_bound`
- PASS — `event_chains_contiguous`
- PASS — `referenced_artifacts_present`
- PASS — `referenced_artifact_hashes_valid`
- PASS — `recovery_observed`
- PASS — `recovery_fencing_monotonic`

## Role attempts

| assignment | attempt | fence | status | error |
| --- | --- | --- | --- | --- |
| asg_bba6768b… | 1 | 1 | completed | — |
| asg_19e22022… | 1 | 1 | lost | worker_process_failed |
| asg_19e22022… | 2 | 2 | completed | — |
| asg_4669b3c5… | 1 | 1 | completed | — |
| asg_be8da97f… | 1 | 1 | completed | — |

## Aggregate state

| type | id | version | state | last action |
| --- | --- | --- | --- | --- |
| workflow | wf_dfe41605… | 6 | CANCELLED | CHILDREN_CANCELLED |
| architecture_revision | arch_2c2d36b3… | 8 | ACCEPTED | HUMAN_ACCEPT |
| execution_epoch | epoch_6b1469c4… | 5 | CANCELLED | NODES_CANCELLED |
| dag_node_run | epoch_6b1469c4… | 3 | CANCELLED | CANCEL_CONFIRMED |
| dag_node_run | epoch_6b1469c4… | 3 | CANCELLED | CANCEL_CONFIRMED |
| dag_node_run | epoch_6b1469c4… | 14 | CANCELLED | CANCEL_CONFIRMED |

## Evidence inventory

- 39 append-only domain events
- 4 durable role invocations
- 20 referenced content-addressed artifacts checked
- 6 durable delivery records

The companion JSON file contains the complete redacted timeline and hashes.
Artifact bodies are intentionally excluded.

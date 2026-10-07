# Historical Documentation

This directory preserves past design decisions, implementation audits and
validation records. Statements about completion, failures, uncommitted changes
or deployment belong to the work described in each report. Use the
[current documentation index](../README.md) for the maintained guides and contracts.

| Area | Records |
| --- | --- |
| Session compaction | [Baseline](session_compaction/BASELINE.md), [gate design](session_compaction/P1_DESIGN.md), [capture/install design](session_compaction/P2_DESIGN.md), [delivery report](session_compaction/DELIVERY.md), [test log summary](session_compaction/test_log_summary.md) |
| LLM projection refactor | [Baseline](llm_projection_refactor/baseline.md), [delivery report](llm_projection_refactor/DELIVERY.md) |
| Prompt cache | [Retired handoff](prompt_cache/prompt_cache_handoff.md), [v2 offline repair](prompt_cache/prompt_cache_v2_offline_repair.md), [mode simplification plan](prompt_cache/prompt_cache_strategy_modes_plan.md) |
| Bunshin V1 | [Implementation notes](bunshin_v1/pal_bunshin_v1.md), [Reviewer gate plan](bunshin_v1/pal_reviewer_gate_plan.md), [layered architect experiment](bunshin_v1/bunshin_layered_architect_planning.md), [state-machine inventory](bunshin_v1/bunshin_state_machine_inventory.md) |
| Migration decisions | [Architecture migration map](migrations/pal_migration_map.md), [advisor removal](migrations/advisor_removal.md) |
| Tool efficiency | [2026-09-23 affordance audit](tool_efficiency/tool_affordance_audit_20260923.md), [Petra sample](tool_efficiency/tool_efficiency_sample_20260923_petra.md), [2026-09-24 sample](tool_efficiency/tool_efficiency_sample_20260924.md), [2026-09-27 audit](tool_efficiency/tool_efficiency_audit_20260927.md), [harness validation](tool_efficiency/harness_efficiency_validation.md) |
| Tool contract review | [Original review](tool_contract_review/Pal_tool_contract_review.txt), [remediation record](tool_contract_review/pal_tool_contract_review_status.txt) |

The original tool-contract review is preserved byte-for-byte. The archived
Markdown reports carry historical-status notices and updated document links.
Original implementation paths, line references and commands inside historical
reports remain evidence of their original checkout.

Formal models, implementation contract matrices and public-proof evidence remain
with their owning directories under `spec/`, `docs/llm_projection_refactor/` and
`docs/evidence/`; their resources are not raw test-output clutter.

Raw root-level test logs are retained byte-for-byte in the ignored local directory
`test-logs/legacy/`. Their original Git revision, result summaries and SHA-256
checksums are recorded in the [test log summary](session_compaction/test_log_summary.md).
New raw test output belongs under `test-logs/` or outside the checkout. Archive
completed reports here when their status is tied to a past implementation;
keep current guides, contracts and actionable design notes in the main index.

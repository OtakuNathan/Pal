# Pal Documentation

This directory contains Pal's user guides, runtime boundaries and subsystem
contracts. Start with the guides below, then follow the contract for the domain
you are changing. Source behavior and a particular installation's loaded state
are separate facts.

Write new and updated documentation in English. Keep original payloads in
machine-readable evidence; label translated historical reports with their
original date and scope.

For current behavior, start with the usage guide and the owning subsystem contract; implementation interfaces can be read in code via links.
`src/pal/*/README.md` describes module ownership, and `spec/` describes the finite model and verification scope.
Historical reports retain their original conclusions and revisions; they cannot be used to judge current capabilities or deployment health.

## Getting Started

1. [What Pal is](what_is_pal.md): product scope, direct conversation, and durable delegation.
2. [Getting started](getting_started.md): installation, runtime selection, connection, upgrade, and source development.
3. [Current implementation notes](current_implementation_notes.md): a short sync point on the current implementation.
4. [Runtime stack](pal_runtime_stack.md): actual modules, ports, and resident/plugin boundaries.
5. [Architecture principles](pal_architecture_v1.md): continuously maintained principles; the filename retains the historical V1.
6. [Bootstrap and process](pal_bootstrap_and_process.md): configuration preparation, assembly, services, and recovery.
7. [Development and validation](development.md): source setup, testing, quality checks and activation.

## Daily Operations and Maintenance

| Task | Entry points |
| --- | --- |
| Local connection, control, and side questions | [TTY](pal_tty.md), [Control](pal_control_plane.md), [/btw](btw_side_questions.md) |
| Tool discovery, schema, and invocation | [Tool surface](pal_tool_surface.md), [Capability Forest](capability_forest_structure.md) |
| Inspecting current state and self-maintenance | [Introspection](pal_introspection_contract.md), [maintenance regressions](pal_self_maintenance_regression.md); operating procedures come from the `pal.self.maintenance` manual in source |
| Plugin installation, prepare, and activation | [Package installation](pal_package_installation.md), [Plugin owner map](../src/pal/plugins/README.md) |
| Channels, providers, and silent output | [Channel contract](pal_channel_contract.md), [Channel owner map](../src/pal/channel/README.md); silent task selection uses the string `"null"` |
| Models, hooks, thinking, and cache | [LLM](pal_llm_contract.md), [cache modes](prompt_cache_modes.md), [cache reminders](pal_cache_reminder.md) |
| Long-running tasks and delivery | [Bunshin orchestration](bunshin_contract_orchestration.md), [direct mode](bunshin_direct_mode.md), [package map](../src/pal/bunshin/README.md), [dependency verification](bunshin_dependency_verification.md) |
| Memory and recovery | [Memory](pal_memory_contract.md), [Memory CLI](pal_memory_cli.md), [dreaming](pal_memory_dreaming.md) |
| Scheduled tasks | [Proactive](pal_proactive_contract.md), [context/destination rules](../src/pal/proactive/README.md) |
| Files, remote, and output recovery | [Remote files](remote_file_tools.md), [batch edits](batch_file_edit.md), [output snapshots](tool_output_snapshots.md), [native observations](native_observation_contract.md) |

Runtime-specific CLIs should always pass `--runtime-root` explicitly. The default selection rule is: existing `~/.pal`
takes priority, then `$PAL_HOME`, and finally `~/.pal`. Editing files is not the same as changing loaded code: rescan discovers metadata,
attach keeps the existing instance, reload replaces the plugin generation, and endpoint restart only reconnects.
Resident/shared implementations require an external host restart.

## Subsystems and Protocols

- [Execution](pal_execution_contract.md): registry generations, strict tool contracts, effect/retry, snapshots, and plugin publication.
- [Turn runtime](turn_runtime_structure.md), [resident scheduling](core_turn_scheduling.md): shared agent kernel, mailbox, admission, teardown, and egress.
- [Prompt context](prompt_context_lifecycle.md), [L1 context index](l1_context_index.md), [continuity](continuity_optimization.md).
- [Core observations](pal_core_events.md): transient event bus, snapshots, and plugin subscriptions; distinct from LLM model/response hooks.
- [Skill](pal_skill_contract.md), [retired behavior boundary](pal_behavior_contract.md).
- [Artifact](artifact_manager.md), [result guidance](pal_result_guidance_contract.md), [failure reporting](pal_failure_reporting_contract.md).
- [MCP](pal_mcp_contract.md), [Web search](pal_web_search_contract.md), [Web fetch](pal_web_fetch_contract.md).
- [Execution extensions](execution_extensions.md): optional native shell and worker contributions.
- [Change admission](pal_change_admission.md): contract changes, generated code, and verification requirements.
- [Formal checks](../spec/README.md): inventory, generated relations, TLC, and conformance; a passing inventory in ordinary CI does not mean TLC has run.

## Design Proposals

The documents below provide design background or directions under discussion; individually they do not constitute implementation status claims. Whether something has landed depends on the current
code and its owning contract; do not infer tool availability from the word plan in a filename.

- [Engineering quality gates](pal_engineering_quality_gates.md)
- [Repair-bill replay](bunshin_repair_bill_replay.md)
- [Approval/access design](pal_approval_access_design.md)
- [Memory/backend CLI plan](pal_memory_backend_cli_plan.md)
- Tool consolidation design notes:
  [plugins](pal_plugin_tool_consolidation_plan.md), [LSP](pal_lsp_tool_consolidation_plan.md),
  [browser](pal_browser_tool_consolidation_plan.md), [execution](pal_execution_tool_consolidation_plan.md)

## Historical Records and Verification Evidence

- [Archive index](archive/README.md): old architecture, migrations, audits, and delivery reports.
- [Former tasking contract](pal_tasking_contract.md): the work-order design superseded by Bunshin Task/workflow.
- [August public-proof script](public_proof_demo.md) and [2026-08-14 evidence](evidence/2026-08-14-public-proof/README.md): a historical recovery/cancellation record; it does not mean Bunshin/workers cannot work today, nor does it serve as evidence of today's end-to-end success.
- [State-machine review](reviews/state_machine_20261009/README.md): a structured report and output recovery notes for a specified revision.
- [Tool-consolidation validation](pal_tool_consolidation_implementation.md), [discovery evaluation](pal_tool_discovery_evaluation_2026-10-15.md): experiments within their respective reported scopes; not a guarantee of production operation.
- [Historical test summary](archive/session_compaction/test_log_summary.md): original tests and checksums; raw logs are kept locally and in Git history.

Write new verification output to the ignored repo-root `test-logs/` or outside the checkout. Keep commands, revisions, results, and
limitations; keep raw logs separate from maintained usage instructions. Historical
failures do not define current capabilities, and historical successes do not
establish the health of a current deployment.

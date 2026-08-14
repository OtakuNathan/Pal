# Pal Documentation

This directory contains Pal's user guides, product-level explanation, and
current V1/V2 architecture contracts.

Older design notes are historical references. These docs are the active baseline for the current implementation.

## Suggested Reading Order

Start here if you want to understand or run Pal:

1. [what_is_pal.md](what_is_pal.md)
2. [getting_started.md](getting_started.md)
3. [current_implementation_notes.md](current_implementation_notes.md)
4. [public_proof_demo.md](public_proof_demo.md)
5. [pal_architecture_v1.md](pal_architecture_v1.md)
6. [pal_runtime_stack.md](pal_runtime_stack.md)
7. [pal_bootstrap_and_process.md](pal_bootstrap_and_process.md)

Continue with the contract for the subsystem you are changing:

- [pal_channel_contract.md](pal_channel_contract.md)
- [pal_change_admission.md](pal_change_admission.md)
- [pal_llm_contract.md](pal_llm_contract.md)
- [pal_execution_contract.md](pal_execution_contract.md)
- [pal_behavior_contract.md](pal_behavior_contract.md)
- [pal_skill_contract.md](pal_skill_contract.md)
- [pal_control_plane.md](pal_control_plane.md)
- [pal_introspection_contract.md](pal_introspection_contract.md)
- [pal_tasking_contract.md](pal_tasking_contract.md)
- [pal_bunshin_v1.md](pal_bunshin_v1.md)
- [pal_engineering_quality_gates.md](pal_engineering_quality_gates.md)
- [pal_reviewer_gate_plan.md](pal_reviewer_gate_plan.md)
- [bunshin_repair_bill_replay.md](bunshin_repair_bill_replay.md)
- [bunshin_layered_architect_planning.md](bunshin_layered_architect_planning.md)
- [bunshin_v2_contract_orchestration.md](bunshin_v2_contract_orchestration.md)
- [pal_proactive_contract.md](pal_proactive_contract.md)
- [pal_memory_contract.md](pal_memory_contract.md)
- [pal_failure_reporting_contract.md](pal_failure_reporting_contract.md)
- [pal_migration_map.md](pal_migration_map.md)
- [pal_web_search_contract.md](pal_web_search_contract.md)
- [pal_web_fetch_contract.md](pal_web_fetch_contract.md)
- [pal_tool_surface.md](pal_tool_surface.md)
- [pal_mcp_contract.md](pal_mcp_contract.md)
- [capability_forest_structure.md](capability_forest_structure.md)
- [turn_runtime_structure.md](turn_runtime_structure.md)
- [pal_approval_access_design.md](pal_approval_access_design.md)

## Document Map

- `what_is_pal.md`: product-level scope, direct turns versus durable Bunshin
  workflows, runtime ownership, and current platform boundaries.
- `getting_started.md`: release installation, setup, service registration,
  connection, dependency checks, upgrades, and source builds.
- `public_proof_demo.md`: a 3–5 minute evidence-led recording and reproduction
  script covering channels, delegation, live restart recovery, and delivery.
- `current_implementation_notes.md`: short current-code sync point for prompt assembly, memory projection, artifacts, tool surface, MCP, and live-state boundaries.
- `pal_architecture_v1.md`: system-level invariants and ownership model.
- `pal_runtime_stack.md`: module skeleton, owning boundaries, and public interfaces.
- `pal_bootstrap_and_process.md`: supervisor, Pal process, bunshins, startup, and runtime composition.
- `pal_channel_contract.md`: channel provider lifecycle, I/O, normalization, reply routing, interaction realization, and UX acknowledgment.
- `pal_change_admission.md`: change admission checklist for contract changes, generated code, dogfood, and review risk.
- `pal_llm_contract.md`: canonical LLM shape, native provider transports, streaming, and model routing.
- `pal_execution_contract.md`: capability, tool, plugin, and execution contract.
- `pal_behavior_contract.md`: affordance descriptors, skill manuals, behavior advice, and the cap-search vs advise split.
- `pal_skill_contract.md`: skill learning, sanitization, STAR applicability, storage, and injection contract.
- `pal_control_plane.md`: explicit control, approval, and governance flows.
- `pal_introspection_contract.md`: self-observation, diagnostics, self-maintenance, and extensibility.
- `pal_tasking_contract.md`: tasking, bunshins, checkpoints, ledgers, and workspace governance.
- `pal_bunshin_v1.md`: implemented bunshin sidecar boundary, approval flow, tasking store, checkpoint cursor, and capability surface.
- `pal_engineering_quality_gates.md`: design baseline for reviewer/verifier gates, LSP evidence, sandbox enforcement, and bunshin engineering-quality hardening.
- `pal_reviewer_gate_plan.md`: historical hardening plan for strict plan and checkpoint reviewer gates; use `pal_bunshin_v1.md#gate-loop` as the current implementation sync point.
- `bunshin_repair_bill_replay.md`: planned repair-bill replay model for propagating downstream integration failures back through the module DAG.
- `bunshin_layered_architect_planning.md`: planned layered architect flow that separates global architecture sketches, per-module detail fill, and implementation milestone planning.
- `pal_proactive_contract.md`: proactive tasks, schedules, run history, and output-channel constraints.
- `pal_memory_contract.md`: L1/L2/L3 memory and memory lifecycle.
- `pal_failure_reporting_contract.md`: developer escalation after self-repair failure.
- `pal_migration_map.md`: current code migration map.
- `pal_web_search_contract.md`: web search provider registry, fallback chain, and capability integration.
- `pal_web_fetch_contract.md`: web fetch subsystem, Playwright rendering, HTTP fallback, and browser service process management.
- `pal_tool_surface.md`: descriptor `invocation_mode`-driven LLM tool exposure, failure surface selection, and discovery-first design.
- `pal_mcp_contract.md`: MCP manager sidecar, config discovery, tool/prompt compilation, and projection lifecycle.
- `capability_forest_structure.md`: unified capability forest structure and compiler model.
- `turn_runtime_structure.md`: turn execution and runtime flow.
- `pal_approval_access_design.md`: deferred approval/access design, access modes, per-turn grants, and endpoint trust boundary.

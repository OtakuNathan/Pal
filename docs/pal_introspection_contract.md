# Pal Introspection Contract

Introspection is a governed self-observation interface. It lets Pal read the current workload, availability,
health, and failure evidence, and select maintenance entry points provided by the responsible owner; it is not a backdoor for directly accessing private objects.
This document describes the current published interfaces; proposals in early designs do not establish tool availability.

## Current entry points

Inspections are organized by object; the specific tools available vary with the registry generation and plugin attachment state.
For the consolidated inspection surface, see [Tool Surface](pal_tool_surface.md#object-scoped-inspection-and-unified-source-inventory).

| Owner | Inspection entry points | Scope |
| --- | --- | --- |
| Core | `observe_core` | resident workload, scheduling, and cache deadline |
| Channel | `inspect_channel_state`, `inspect_channel_endpoint(name)` | Hub inventory; lifecycle/auth/health/backlog of one endpoint |
| Plugins | `inspect_plugin()`, `inspect_plugin(name)`, `list_plugins` | host state, single-plugin generation/dependencies/cleanup/errors, or the inventory |
| LLM | `inspect_llm()` | all loaded enabled endpoints, active label, thinking, and process usage |
| Memory | `inspect_memory_state` | L1/L2, active provider availability, and bounded counts; detailed maintenance goes through the CLI |
| LSP | `inspect_lsp_status` | host; readiness diagnosis per the tool contract when a workspace/server is specified |
| MCP | `inspect_mcp_state`, `read_mcp_server(name)` | manager state; config, errors, and discovery snapshot of a specified server |
| Proactive | `inspect_proactive_status`, `read_proactive_task(name)` | scheduler and a specified definition/latest run; history is queried separately |
| Artifact | `inspect_artifact_state`, `inspect_artifact_info` | policy, caller-visible resources, and a specified artifact lifecycle |
| Failure | `inspect_failure` | bounded latest reports and full result snapshots |

An inspection returns the facts known about its object and does not recursively expand the entire runtime. Large histories, business content, and
server tool inventories still use separate queries.

The canonical `introspection` namespace is for internal organization and is not a model-facing calling path.
Models use public aliases; nothing should be inferred from old designs such as `introspection.self_diagnose`
or a per-module `observe/modify/debug/extend` quartet.

## Evidence and unavailable

- Read success and object health are separate. A successful inspection call does not mean the service is healthy.
- When some sources fail, known information in other sections should still be preserved.
- `diagnostics` expresses observation time, owner/source, partial coverage, and unavailable
  sections; unknowns are not filled in as healthy, zero-effort, or empty errors.
- Persisted metadata can describe prior configuration but cannot prove the current process, socket, plugin
  attachment, or capability availability.
- LSP/MCP inspections do not automatically start sidecars just to read state; active diagnosis requires an explicit tool selector.
- Some existing inspections perform owner-internal bookkeeping, such as artifact hot-state refresh;
  this does not expand the caller's mutation authority.
- Secrets/tokens are not returned through introspection; auth state only expresses configuration and authorization status.

## Self-maintenance entry points

Configuration, code files, loaded generations, and validation behavior are four distinct states. Before maintaining, read the
[built-in manual](../src/pal/skill/builtin_skills.py) at `pal.self.maintenance`,
and cross-check the checkout, installed interpreter/packages, runtime root, and actual services.

| Change | Current owner and activation boundary |
| --- | --- |
| Plugin/provider implementation | PluginHost's `manage_plugin(operation="reload")`; check load/cleanup errors |
| New plugin or manifest | discovered by `rescan_plugins`, then attached/enabled per the enabled state; do not repeat activation already done by the installer |
| Single endpoint connection | `manage_channel_endpoint(operation="restart")`; do not re-import provider code |
| LLM endpoint configuration | after `pal llm` / `pal setup --llm`, load via `/refresh_llm_endpoint`, affecting future turns |
| Identity refreshable fields | `pal identity refresh` after authorized edits to durable config; system name/core policy remain startup-pinned |
| Memory backend/index | resident owner controls of `pal memory`; embedding-space changes use plan/migrate |
| Resident/shared implementation | after updating and verifying the actual installed source, the operator/external supervisor restarts the host |

Runtime-specific CLIs use an explicit `--runtime-root`. Online owner failures must not be silently bypassed
with direct SQLite/config writes. A full setup changes multiple domains and is unsuitable as a single configuration patch.

## Self-Maintenance Loop

```mermaid
flowchart LR
    OBS["Observe current owner state"] --> DIAG["Locate cause and impact scope"]}
    DIAG --> FIX["Prepare fixes within authorized scope"]
    FIX --> CHECK["Verify contract and behavior"]
    CHECK --> ACT["Activate via the owning lifecycle"]
    ACT --> VERIFY["Check load results and representative behavior"]
    VERIFY --> MEMORY["Save reusable experience or report the remaining failure"]
```

This is a working method, not a new workflow engine or aggregate diagnostics API. Small fixes can be done in an ordinary
turn; Bunshin can be used when long-term collaboration or independent verification is needed. A diagnostic request does not automatically authorize
unrelated configuration or production changes, and explicit authorization already given does not need to be asked again.

Defer additional activations while the Pal is busy. Do not stop, kill, or restart
the Pal's own host from within the Pal's own active turn, or schedule a delayed restart of it; external restarts are left to the operator/supervisor.
A failed reload does not guarantee that the old generation has been restored; the real lifecycle/cleanup status must be reported.

## Relationship to Failure Reporting

Failures keep the original cause, exception chain, and recovery hints with the corresponding owner. Maintenance work that cannot be fixed, exceeds authorization,
or has been stopped is aggregated as evidence per [Failure Reporting](pal_failure_reporting_contract.md);
do not use "a reload was attempted" in place of "behavior has recovered," and do not treat historical failures as current health.

## Invariants

- Self-observation goes through public ports/capabilities; do not read other owners' private implementations to guess state.
- Inspection does not expand execution permissions and does not replace approvals or configuration owners.
- Report configuration existence, file copying, code loading, and behavior verification separately.
- States that cannot be obtained are explicitly unavailable; do not fabricate health, success, or zero effort.
- The live registry is authoritative for current tools; historical designs do not automatically become callable interfaces.

# LSP Tool Surface Consolidation Plan

Date: 2026-10-15
Status: Implemented to source per the confirmed scope; targeted verification is in the [implementation record](pal_tool_consolidation_implementation.md). Not yet installed or activated.

## Confirmed Direction

LSP continues to provide online tools per plugin capabilities; `rescan_lsp_servers` is retained and not migrated into the CLI.

Following the principles of the [Browser tool surface consolidation plan](pal_browser_tool_consolidation_plan.md), prioritize merging duplicate diagnostics and queries with identical parameters; do not build a universal super entry with complex conditional parameters.

The target is public tools **15 → 10**; after `prepare_lsp_workspace` is demoted from resident direct-call to indirect tool, LSP no longer occupies a resident direct-call slot. The following are targets, not yet in effect.

## Current Baseline

Per the capability declarations in `src/pal/lsp/plugin.py`, there are currently 15 tools, with only `prepare_lsp_workspace` as resident direct-call. This is the source-code view, not a snapshot of a running instance's tools.

| Capability group | Existing tools | Count |
| --- | --- | ---: |
| Health diagnostics | `inspect_lsp_provider`, `inspect_lsp_status`, `diagnose_lsp_server` | 3 |
| Project environment preparation | `prepare_lsp_workspace` | 1 |
| Errors and documentation | `read_lsp_diagnostics`, `read_lsp_hover` | 2 |
| Symbol locations | `find_lsp_definitions`, `find_lsp_implementations`, `find_lsp_references` | 3 |
| Call relations | `prepare_lsp_call_hierarchy`, `find_lsp_incoming_calls`, `find_lsp_outgoing_calls` | 3 |
| Symbol catalog | `list_lsp_document_symbols`, `search_lsp_workspace_symbols` | 2 |
| Configuration refresh | `rescan_lsp_servers` | 1 |
| Total | | **15** |

## Planned Changes

### Health diagnostics: 3 → 1

Keep a single LSP inspection entry, queried by object scope:

- With no inner object specified, query module and manager health, necessary counts, and server summaries, without expanding full details for every workspace/server by default.
- When a workspace is specified, query that workspace's environment preparation records, related server status, and failure causes, without reading details of unrelated workspaces.
- When a specific server is specified, query the full relevant diagnostics for that server, including binary availability, project context, initialization result, and failure causes. Keep existing server selection capability; the final selection parameters are determined at implementation time, avoiding duplicate equivalent fields such as file/path.

The current three tools are not fully identical: provider inspection checks the resident host state, workspace status queries existing records, and doctor checks and may initialize the selected server.

The merge must explicitly preserve this behavioral boundary: default module and workspace summary queries remain passive and do not start the manager or all servers due to inspection; active diagnostics for a specific server target only the explicitly selected object. At implementation time, verify execution classification, role permissions, and error semantics; initialization effects must not be hidden inside ordinary global state reads.

When the manager is unavailable, still return whatever host state and failure cause can be obtained; cached observations are explicitly marked as historical and are not evidence of a current run or successful query. A missing workspace preparation record does not mean all LSP queries are unavailable.

The final public name is determined at implementation time, preferring reuse of `inspect_lsp_status`. Retire the other two standalone diagnostic aliases, and fix all recovery guidance accordingly.

### Call hierarchy preparation: 1 → 0

Remove model-callable `prepare_lsp_call_hierarchy`.

The existing incoming/outgoing queries already execute `textDocument/prepareCallHierarchy` internally and query all returned candidate items; their results also include items. Keep this internal preparation flow; the model does not need to run preparation separately before querying call relations.

Preserve the real meaning of empty results and multiple candidates; do not arbitrarily pick the first candidate, and do not interpret an empty result as a server health failure.

### Call relation queries: 2 → 1

Merge `find_lsp_incoming_calls` and `find_lsp_outgoing_calls` using one required direction enum:

```text
direction = callers | callees
```

Share the existing file, line, character, plus optional workspace and server selection parameters. The two directions do not switch required or mutually exclusive fields; only the selected direction is executed, and both directions are not queried together by default.

Preserve the LSP position conventions: line numbers start at 0 and character is a 0-based UTF-16 offset; preserve provenance, file version, and actual query results.

The final public name is determined at implementation time; no searchable old direction-specific aliases are kept.

### Definition and implementation queries: 2 → 1

Merge `find_lsp_definitions` and `find_lsp_implementations` using one required query-type enum:

```text
kind = definition | implementation
```

Share the previously identical file location, workspace, and server selection parameters. Return definition locations or concrete implementation locations per the selection; do not query and return both result sets by default.

`find_lsp_references` is kept standalone: it queries usage locations and has its own `include_declaration` parameter; that parameter is not added to the new query tool to create conditional combinations.

The final public name is determined at implementation time; no standalone tool aliases for the two old operations are kept.

### Project environment preparation demoted to indirect tool

`prepare_lsp_workspace` keeps its existing environment configuration and on-demand warmup capabilities, including the compilation database, include paths, standards, and fallback flags, but is demoted from resident direct-call to indirect tool.

Ordinary navigation and diagnostics call the needed tools directly when the project environment is available; the runtime starts or reuses servers. Tool search, static descriptions, and failure recovery make this explicit; prepare is not turned into a fixed step before every piece of work.

When environment preparation succeeds, continue to report facts only; on partial failure or failure, provide an inspection recovery entry bound to the current workspace and, when necessary, to the current server, instead of repeating the full tool menu in every result.

### Configuration refresh keeps an online entry

`rescan_lsp_servers` continues as an indirect plugin tool, refreshing configuration and health status after LSP server configuration is added or modified.

Its responsibilities remain distinct from plugin reload: rescan reads configuration, reload replaces plugins and manages the manager lifecycle; neither may impersonate the other. Also update the diagnostic and verification entry points in the LSP template development skill; do not migrate them into the CLI.

## Independent capabilities explicitly retained

- `read_lsp_diagnostics`: compilation or type errors and warnings for source files; not merged into server health diagnostics.
- `read_lsp_hover`: types, signatures, and documentation at a known location.
- `find_lsp_references`: reference locations of a symbol.
- `list_lsp_document_symbols`: symbol structure of a single file.
- `search_lsp_workspace_symbols`: query project symbols by name; uses different inputs from the file symbol catalog and is not merged.

A successful empty result continues to mean no corresponding evidence was returned; it does not automatically trigger preparation, rescan, or health diagnostics.

## Target tool surface accounting

| Adjustment | Reduction |
| --- | ---: |
| Consolidating three kinds of health diagnostics | 2 |
| Removing the standalone call-hierarchy preparation | 1 |
| Merging the two call directions | 1 |
| Merging definition and implementation queries | 1 |
| Total | **5** |

Target of 10 entry points: unified inspection, workspace prepare, file diagnostics, hover, definition/implementation query, references, call relationship query, file symbols, project symbols, rescan. All are indirect tools; final names and full schemas are determined at implementation time.

## Pre-implementation checks and acceptance

1. Determine the new public names, input contracts, and execution classification; inventory references in providers, generation models, the manager, skills, recovery affordances, documentation, and tool discovery tests.
2. Retire the old public entry points without mistakenly deleting protocol operations, shared implementations, or Bunshin/worker client-only lifecycle constraints still needed inside the manager.
3. Perform targeted verification of passive hub/workspace checks, manager unavailability, historical caches, proactive checks on a specified server, unrelated workspace isolation, failure recovery, and role permissions.
4. Perform targeted verification of actual routing for both query enums, enum rejection of invalid values, internal call-hierarchy preparation, processing of all candidates, successful empty results, and source and file version evidence.
5. Verify that the native schema, search catalog, and contract reads show only the new tool surface; prepare is discoverable but not resident; rescan remains callable online.
6. Model validation must check recall, enum selection, argument validity, final results, and call rounds together; do not claim improvement directly from the reduced tool count.
7. Run the relevant targeted tests locally; full CI is left to GitHub. Record implementation, file installation, and running-instance activation separately; schedule activation according to the plugin's actual reload boundary.

The confirmed scope of this plan has been implemented in source code; installation and running-instance activation have not yet been performed.

## Related module plans

- [Browser tool surface consolidation plan](pal_browser_tool_consolidation_plan.md)
- [Execution tool surface consolidation plan](pal_execution_tool_consolidation_plan.md)
- [Plan for migrating memory backend maintenance into the CLI](pal_memory_backend_cli_plan.md)
- [Emotion broadcast and tool surface merge plan](pal_emotion_broadcast_plan.md)

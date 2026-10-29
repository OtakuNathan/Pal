# Plugins Tool Surface Consolidation Plan

Date: 2026-10-15
Status: Implemented into source code per the confirmed scope; for targeted verification see [Implementation Record](pal_tool_consolidation_implementation.md). Not yet installed or activated.

## Agreed Direction

Plugin management continues to provide online capabilities. Following the principles of the [Browser tool surface consolidation plan](pal_browser_tool_consolidation_plan.md), merge lifecycle operations and scan operations that share parameters, and unify hub and single-plugin diagnostics; package management keeps its separate entry points.

Public tool target **14 → 8**. After `list_plugins` is downgraded from resident direct call to an indirect catalog tool, plugins no longer occupies a resident direct-call slot. The above is the implementation target and is not yet in effect.

## Current Baseline

Per the capability declarations in `src/pal/plugins/capabilities.py`, there are currently 14 tools, with only `list_plugins` as a resident direct call. This is the source-code view, not a snapshot of a live instance's tools.

| Capability group | Existing tools | Count |
| --- | --- | ---: |
| Query and diagnostics | `inspect_plugin_host`, `list_plugins`, `inspect_plugin` | 3 |
| Lifecycle | `attach_plugin`, `detach_plugin`, `reload_plugin`, `enable_plugin`, `disable_plugin` | 5 |
| Scan discovery | `rescan_plugins`, `rescan_and_attach_first_party_plugins` | 2 |
| Package management | `install_package`, `prepare_package`, `uninstall_plugin`, `inspect_package_status` | 4 |
| Total | | **14** |

## Lifecycle: 5 → 1

Unified as `manage_plugin`, with a shared required plugin name and one required operation enum:

```text
manage_plugin(name=..., operation=attach | detach | reload | enable | disable)
```

No operation-specific parameters are added. The model must still explicitly choose an action; the merge reduces discovery entry points and does not mean the five lifecycle semantics are interchangeable.

| operation | Behavior retained |
| --- | --- |
| `attach` | Load an enabled plugin; keep instances that are already loaded, with no implicit code refresh |
| `detach` | Temporarily unload the instance while preserving the enabled state |
| `reload` | Replace an enabled plugin's implementation and its owned resources, handling affected dependent plugins per existing rules; an enabled plugin that has been unloaded may be loaded |
| `enable` | Enable and load, persisting the enabled state per the original success conditions |
| `disable` | Unload and persist the disabled state, preserving the original handling when unloading fails |

The runtime explicitly dispatches to the existing host lifecycle methods and does not automatically upgrade attach to enable or reload because of the merge. The original semantics of dependency recovery, logical unload committed, resource cleanup incomplete, failure receipts, and uncertain results are preserved.

Revoke the five old model entry points and keep no searchable duplicate management aliases. Host internal methods and lifecycle interfaces used by other owners remain in place.

Model calls use the public action `reload`; the host's existing internal `reattach` implementation name may remain unchanged, and the internal protocol is not additionally renamed for the unified tool surface.

## Hub and Single-Plugin Diagnostics: 2 → 1

Consolidate `inspect_plugin_host` and `inspect_plugin` into `inspect_plugin(name?)`:

- Without a name: return a health summary of the hub itself, plugin and generation counts, scan errors, and pending-cleanup counts, without expanding the full status of all plugins by default.
- With a name: return the full relevant diagnostics for that plugin, including enabled/attached, lifecycle state, generation, dependencies and blocking reasons, cleanup progress and errors, install status, and recent errors.
- Dependencies are shown as references, without recursively expanding the full status of other plugins.
- When the object is not found, report unknown plugin and guide discovery of the name via the plugin catalog.

Keep the existing single-plugin inspection's handling of configuration values, and do not output full sensitive configurations to the model by default as a result of the consolidation. When install status is temporarily unavailable, keep the other available diagnostics and clearly mark the missing part.

## Plugin Catalog Retained, Downgraded to an Indirect Tool

`list_plugins` continues to return available plugin names, owning modules, first-party/third-party origin, and brief enabled, attached, and lifecycle states, without returning full details for each plugin.

It is responsible for discovering specific plugin objects; `search_tools(domain="plugin")` is responsible for discovering plugin management tools, and the two cannot directly replace each other.

Revoke the resident direct-call status of `list_plugins`, keeping indirect invocation and search discovery. Maintenance skills and recovery guidance reference the catalog as needed, rather than repeatedly requiring a plugin listing after every successful operation.

## Scan Discovery: 2 → 1

Keep a single `rescan_plugins` with a single mode enum:

```text
mode = scan | scan_and_attach_new_first_party
```

The two modes do not switch additional parameters. The default remains scan-only; the auto-attach mode must be explicitly selected by the caller.

- `scan`: Detect directory and manifest metadata changes, preserving the existing legacy channel provider discovery path; do not implicitly reload running code.
- `scan_and_attach_new_first_party`: After performing the same scan, automatically attach only first-party plugins newly discovered in this scan that are enabled.

Do not extend to reloading existing plugins, enabling disabled plugins, or auto-attaching third-party plugins. Keep scan errors and attach errors separate; partial completion must not be claimed as full activation success; preserve the existing error states and effect receipts.

Revoke the separate model entry point `rescan_and_attach_first_party_plugins`. The underlying composite implementation may continue to be used.

## Four Package Management Entry Points Retained

| Tool | Reason retained |
| --- | --- |
| `install_package` | Install or upgrade from a local package path, prepare private dependencies, and activate via the owning owner |
| `prepare_package` | Prepare or repair dependencies by the name and type of an already-installed object |
| `uninstall_plugin` | Removes third-party plugins, with independent data cleanup options and lifecycle constraints |
| `inspect_package_status` | Queries the phase, failure, and activation results of asynchronous jobs, including cases where the plugin is not yet registered or has already been uninstalled |

Do not mix different path/name/kind/purge_data parameters into one generic package operations tool. Installation state for single-plugin inspection and diagnostics for a specific asynchronous job keep their own object boundaries.

Keep the existing completion notification, the bounded wait when there is no notification, and the result verification path; successful results do not append repeated polling or a full tool menu.

## Target Tool Surface

| Target entry point | Count |
| --- | ---: |
| `manage_plugin` | 1 |
| `inspect_plugin` | 1 |
| `list_plugins` | 1 |
| `rescan_plugins` | 1 |
| Four existing package management entry points | 4 |
| Total | **8** |

Accounting: lifecycle reduced by 4, inspection reduced by 1, scanning reduced by 1, for a total reduction of 6. All target entry points are indirect tools.

## Pre-Implementation Checks and Acceptance

1. Determine the input contract and execution classification, and inventory old entry point references in the host, public providers, skills, prompts, affordances, package management activation flows, documentation, and tests.
2. Revoke only model-facing entry points, keeping the internal lifecycle owner and the host interfaces that package management depends on. Update maintenance guidance, including how plugin reload is invoked in `pal.self.maintenance`; do not mix channel endpoint reconnection into plugin generation replacement.
3. Perform targeted verification of routing for the five actions and the original enabled/attached persistence, dependency handling, reload code replacement, logical uninstall receipts, cleanup failure, and recovery behavior.
4. Verify hub and single-plugin diagnostic scopes, directory name discovery, partial failures in installation diagnostics, and unknown plugin handling.
5. Verify the scope of scan-only and auto-attach for new third-party plugins, covering disabled plugins, existing plugins, third-party plugins, scan errors, and attach errors.
6. Confirm that native schema, tool search, and full contract reads expose only the target tool surface; role permissions continue to constrain accessible plugins and lifecycle actions, and authorization must not be loosened because of the consolidation.
7. Model evaluation checks tool discovery, enumeration choice, parameter validity, real lifecycle results, and call turns together; do not claim improved effectiveness based solely on the reduced name count.
8. Run only the relevant targeted tests locally, leaving full CI to GitHub. Record source file modifications, installed file copies, running instance activation, and actual behavior verification separately; do not append activation requests while Pal is busy.

The confirmed scope of this plan has been implemented in the source code; installation and running instance activation have not yet been performed.

## Related Module Plans

- [Browser Tool Surface Consolidation Plan](pal_browser_tool_consolidation_plan.md)
- [Execution Tool Surface Consolidation Plan](pal_execution_tool_consolidation_plan.md)
- [LSP Tool Surface Consolidation Plan](pal_lsp_tool_consolidation_plan.md)
- [Memory Backend Maintenance Migration to CLI Plan](pal_memory_backend_cli_plan.md)
- [Emotion Broadcast and Tool Surface Consolidation Plan](pal_emotion_broadcast_plan.md)

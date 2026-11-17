# plugins

Owns:
- plugin package namespace
- plugin host and bundle discovery
- plugin truth for third-party bundles
- built-in and third-party plugin loading boundaries
- plugin registries and first-party plugin implementations

Does not own:
- memory semantics
- execution governance
- runtime orchestration

Exposes:
- `PluginHost`
- `PluginBundleRepository`
- `L3PluginRegistry`
- built-in L3 plugin bundles

Plugin naming rules:
- Plugin authors declare clear source names through `path_module_id`, `module_id`, `family`, and `action_name`.
- Keep `path_module_id` and `family` short, stable, and reusable across plugins.
- Reuse shared abbreviations such as `mgmt` and `disc` instead of inventing plugin-local variants.
- Keep `action_name` descriptive; the compiler owns canonical path abbreviation.
- Do not handcraft abbreviated canonical paths in plugins. New high-frequency abbreviations must be added centrally in the compiler.

Tool failures are authored by the plugin's execution boundary. Return concise
`llm_text` with the root cause and supply the existing effect receipt, retry
directive, recovery hint or affordances. `ToolExecutionError` and
`ToolRejectedError` carry owner-authored public reasons; chained exceptions are
retained in service logs. Do not embed traceback reports in public messages.
Use `exception_reason` for local exceptions, `diagnostic_reason` for external
failure text, and `render_diagnostic_record_for_llm` for diagnostic records.
Keep command output, file content and other successful business data intact.
MCP adapters follow the same rule, marking effects unknown when the server
does not supply a trustworthy receipt. Human control replies use the same
cause and include recovery only when the user needs to act.

Plugins can observe resident state through `scope.subscribe_core_events(...)`.
Subscriptions become active on publication and close with the plugin generation.
See [Core system observations](../../../docs/pal_core_events.md) for the bounded
mailbox, snapshot and worker cleanup contract.

Channel providers are plugin contributions. PluginHost owns their generations,
cleanup retries and import eviction; resident ChannelRuntime owns endpoint hubs
and queued delivery across generation replacement. See the
[channel plugin contract](../channel/README.md). Legacy runtime-root
`channel/providers/*/provider.toml` packages are adapted without moving their
files or endpoint data. Manage them with the same plugin tools as other bundles.

Plugin lifecycle:
- Source builtin manifests live in `src/pal/plugins_builtin/`. Provisioning syncs
  manifests into `<runtime_root>/plugins/_builtin/`; implementations still load
  from the installed Pal package. Editing a copied manifest does not update code.
- New community bundles use `<runtime_root>/plugins/community/<plugin_id>/plugin.toml`.
  `build_plugin(context)` constructs an instance without starting resources;
  `start(scope)` acquires resources and returns a `ModuleHandle`.
- `rescan_plugins` discovers metadata. Attach loads an enabled detached plugin
  and preserves an attached instance; reload replaces its generation; enable
  also activates a disabled plugin. There is no automatic filesystem reload.
- Publication waits for successful startup. Cleanup runs in reverse ownership
  order; failed cleanup keeps ownership for retry. Failed reload is not a
  promise of rollback. Inspect lifecycle results, errors and representative
  behavior before claiming activation.
- Resident modules and shared implementation changes need an external host
  restart; plugin reload cannot replace their imported dependencies.

Slash commands are a native generation-owned contribution:

```python
controls = scope.context.port_registry.observe(CORE_CONTROL)

async def pair(action):
    result = await phone_service.pair()
    await controls.current.reply(action, result.message)

return ModuleHandle(
    module_id="phone", tier=MODULE_TIER_DETACHABLE, detachable=True,
    control_commands=(ControlCommandSpec(
        name="pair_phone", description="Pair a new phone", handler=command,
        show_in_panel=True, panel_button=True, panel_label="Pair phone",
    ),),
    control_action_handlers={"phone_pair": ControlActionSpec(pair)},
)
```

Here `command(invocation)` returns a `ControlAction` with action kind
`phone_pair`, `target_scope="phone"` matching the module id, and the invocation's
route. Import `CORE_CONTROL` from `pal.control.runtime_contracts` and
`ControlActionSpec` from `pal.core.control_action_registry`. Declare `core:control`
in `requires_ports` when using its query/delivery interface. Business handlers are
async and return `None`; they deliver their own result through this Core port.
Custom button parsers are declared in `ModuleHandle.control_interactions` as
`ControlInteractionSpec` values. PluginHost validates and publishes these declarations, cleans them
up on withdrawal or rollback, and refreshes the common channel catalog. Do not
manually mutate the router or add unregister callbacks from plugin start.

Modules can also declare a `timer_scope` opened through `CORE_CONTROL` and
`configuration_handlers` for resident configuration RPCs. Publication and
withdrawal own those contributions alongside commands and callbacks. A timer
only enqueues an event; its callback runs on the main loop, and retired owners
cannot execute queued timers or withdraw a replacement's configuration handlers.
The optional [cache reminder](../cache_reminder/README.md) is a first-party
example using those facilities without a feature-specific PalCore dependency.

Package preparation and online installation can already perform activation;
inspect their result before adding another lifecycle request. See
[package installation](../../../docs/pal_package_installation.md) and the
[self-maintenance manual](../skill/builtin_skills.py).

Modules can contribute `module_observer` for synchronous derived projections of
published module generations. Core owns replay and identity-checked retirement;
Skill consumes this seam to maintain declarations. Metadata refresh uses the
same publication methods as initial loading, including Execution replacement.

`PluginHost.package_lifecycle` owns runtime replacement for both plugin and
channel-provider packages: entry withdrawal, writer admission, detach, retained
cleanup, activation and restoration. Packages stage and switch files through
that boundary, and do not stop Channel transports or inspect private generation
maps. The same owner supplies uninstall cleanup observations.

An admitted background turn retains its exact module generation. Unload and
package replacement reject an owner with active background work before cleanup
starts; finish or interrupt that work and retry. Other modules remain available
for lifecycle changes from the same turn, including exclusive tool operations.

Timers and ordinary event handlers validate their exact module registration
when triggered. Unload or replacement makes a pending callback a no-op for that
dispatch; it is neither retried nor sent to the new generation. This applies to
callbacks that have not entered yet. A coroutine already executing may continue
after unload, so callback admission does not supply an execution lifetime lease.

Each PluginScope owns an explicit registration state. Only a scope whose exact
handle acquired the module name may withdraw shared contributions; an unclaimed
attempt closes its private resources. Once public withdrawal succeeds, cleanup
retries stay private even if another plugin has acquired the same module name.
Event-source ids must be unique among live registrations, and source withdrawal
requires the original instances. See [registration ownership](../../../docs/module_registration.md)
for the production state machine and its generated TLA+ relation.

An occupied manifest module id is rejected before candidate startup. L3 provider
names belong to `l3.<provider_id>`; Execution references belong to their declared
`<module_id>:<local_name>` namespace. Both reject duplicate claims and require
the exact previous instance for release. L3 publication and data selection belong
to Memory; plugins do not restore global selection from cleanup callbacks. See
[managed Memory swap](../../../docs/pal_memory_swap.md).

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

Package preparation and online installation can already perform activation;
inspect their result before adding another lifecycle request. See
[package installation](../../../docs/pal_package_installation.md) and the
[self-maintenance manual](../skill/builtin_skills.py).

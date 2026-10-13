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

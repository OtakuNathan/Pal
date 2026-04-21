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

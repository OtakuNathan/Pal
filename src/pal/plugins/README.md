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

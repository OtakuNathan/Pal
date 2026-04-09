# plugins/l3

Owns:
- pluggable L3 provider registry
- built-in L3 stub providers

Does not own:
- L1/L2 runtime memory
- memory service orchestration
- plugin loading beyond L3 registry scope

Exposes:
- `L3PluginRegistry`
- `NullL3Plugin`
- `MockL3Plugin`
- `SQLiteFTSL3Plugin`

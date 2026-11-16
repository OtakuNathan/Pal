# plugins/l3

Owns:
- built-in L3 stub providers

Does not own:
- L1/L2 runtime memory
- memory service orchestration
- provider selection and publication (owned by Memory)
- plugin loading beyond the L3 contribution

Exposes:
- `NullL3Plugin`
- `MockL3Plugin`
- `SQLiteFTSL3Plugin`

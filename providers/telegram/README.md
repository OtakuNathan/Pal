# Telegram channel provider

This provider is loaded exclusively from
`<runtime_root>/channel/providers/telegram/`.

Its runtime-owned implementation and manifest live in that provider directory.
Each configured endpoint keeps provider-owned mutable state in
`<runtime_root>/data/channel/<endpoint_id>/state.sqlite3`. The central Pal
database owns only endpoint registration and lifecycle projection.

Inline-keyboard callback mappings and native Telegram message targets are
durable provider projections. Human-review truth and decision validity remain
owned by Minion.

# Telegram channel provider

This provider is loaded exclusively from
`<runtime_root>/channel/providers/telegram/`.

Its runtime-owned implementation and manifest live in that provider directory.
Each configured endpoint keeps provider-owned mutable state in
`<runtime_root>/data/channel/<endpoint_id>/state.sqlite3`. The central Pal
database owns only endpoint registration and lifecycle projection.

Inline-keyboard callback mappings and native Telegram message targets are
durable provider projections. Human-review truth and decision validity remain
owned by Bunshin.

Build and install the provider independently from Pal core:

```bash
scripts/build_provider_packages.sh
pal provider install ./dist/providers/pal_channel_provider_telegram-*.whl \
  --runtime-root ~/.pal
```

The wheel is a versioned provider artifact, not a request to install its code
into Pal's shared Python environment. `pal provider install` validates and
atomically publishes its payload under the runtime root, archives the previous
copy, and leaves endpoint configuration, credentials, and provider data alone.
Run `channel_provider_rescan` in Pal (or restart the service) to activate the
new generation.

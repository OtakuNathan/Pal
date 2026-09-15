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

Remote execution approval uses the same execution-owned approval flow as TTY
and desktop_avatar. The card is delivered only to the channel that requested
the operation. Telegram callbacks must come from the paired user who owns the
card, in its original chat; the verified Telegram sender becomes the approval
actor. Approval is single-use and expires. “Approved once” confirms authorization,
not successful execution; the operation reports its result separately.

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
The CLI installation requires Pal to be stopped; start it afterward to load the
new generation. For online installation use `package_install` inside Pal and
check `package_status` for the coordinated activation result. For an existing
provider updated directly on disk, use `channel_reload_provider` with
`name="telegram"`; rescan alone does not reload existing provider code.

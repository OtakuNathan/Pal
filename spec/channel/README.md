# Channel state-machine models

`EndpointHubLifecycle.tla` models one physical channel endpoint plus the
non-detachable recovery socket. It checks the EndpointHub lifecycle, late/early
capability publication, ordered in-process backlog ownership, transport
acknowledgement before replacement/removal, physical removal, and delivery-only
fallback to the socket. Unacknowledged transport work returns to the hub before
detach, so a later attach can replay it without a transport generation object.
The model's `buffer` count abstracts both typed hub deliveries and raw frames
that have returned from a stopped transport; physical removal rewrites the
latter as recovery-socket notifications before transferring ownership.
Likewise, the model's `delivered` counter advances only on transport ownership
acknowledgement. It is intentionally stricter than the runtime diagnostic
`EventKind.REPLY_DELIVERED`, which records endpoint acceptance.

`EndpointHubImplementationReducer.tla` is generated from the executable Python
reducer in `pal.channel.lifecycle`. Each relation row contains the lifecycle
state, physical presence, transport presence, publication, publication intent,
buffer guard, action, and complete target snapshot. The runtime and TLC
therefore use the same reducer rather than parallel hand-written side effects;
the lifecycle model adds the multi-endpoint protocol and delivery invariants.

Run the model with the pinned TLC jar:

```bash
scripts/check_channel_tla.sh tla2tools.jar
```

Regenerate the implementation relation after changing the Python reducer:

```bash
python -c "from pathlib import Path; from pal.channel.formal import render_endpoint_hub_implementation_relation; Path('spec/channel/EndpointHubImplementationReducer.tla').write_text(render_endpoint_hub_implementation_relation(), encoding='utf-8')"
```

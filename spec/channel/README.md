# Channel state-machine models

`EndpointHubLifecycle.tla` models one physical channel endpoint plus the
non-detachable recovery socket. It checks the EndpointHub lifecycle, late/early
capability publication, ordered in-process backlog ownership, physical removal,
and delivery-only fallback to the socket.

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

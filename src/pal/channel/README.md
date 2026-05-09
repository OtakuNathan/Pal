# channel

Owns:
- endpoint runtime instances and their parent management layer
- inbound mailbox and outbound outbox runtime
- normalize, reply routing, and response handles
- adapter registry for legacy/transport compatibility
- delivery diagnostics for queued replies

Does not own:
- memory truth
- tasking truth
- control reasoning
- execution side effects

Exposes:
- `EndpointConfig`
- `ResponseHandle`
- `ChannelEnvelope`
- `ChannelEndpointBase`
- `ChannelEndpointQueueBase`
- `ChannelRuntime`
- `ChannelAdapterRegistry`
- `ChannelEndpointRegistry`
- `ChannelEventSource`
- `QueuedReply`

Interaction rule:
- channel normalizes external ingress before it reaches `PalCore`
- reply completion for a turn means "accepted by channel outbox"
- actual delivery success/failure is emitted later as channel-side diagnostics
- concrete endpoint implementations inherit `ChannelEndpointQueueBase` and fill in:
  - `normalize_raw(...)`
  - `send_reply(...)`
  - `inspect_health(...)`
  - `inspect_auth_state(...)`
- `ChannelRuntime` is the parent manager for endpoints:
  - it lists endpoints
  - enables/disables them
  - attaches/detaches them
  - reloads one endpoint provider implementation while keeping the channel bus mounted
  - polls each endpoint mailbox
  - flushes each endpoint outbox
- endpoint nodes expose only their own introspection/configuration surface:
  - `inspect`
  - `auth_state`
  - `set_auth_material`
  - `health`
  - `backlog`
- endpoint nodes do **not** expose `attach/detach`; those are channel-parent
  management actions
- channel root is a core bus and is not hot-reloaded as a module; provider/endpoint
  implementations are the hot-reload boundary
- no channel operation surface is exposed to `Pal/LLM`
- secrets/tokens are write-only; introspection never returns them

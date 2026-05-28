# channel

Owns:
- endpoint runtime instances and their parent management layer
- inbound mailbox and outbound outbox runtime
- normalize, reply routing, and response handles
- adapter registry for legacy/transport compatibility
- provider manager for plugin-contributed channel providers
- delivery diagnostics for queued replies
- channel-neutral interaction status and callback mapping

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
- `ChannelEndpointProviderManager`
- `ChannelProvider`
- `ChannelProviderContext`
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
- concrete endpoints may override the channel-neutral interaction hooks:
  - `apply_control_catalog(...)`
  - `apply_interaction_status(...)`
  - `open_or_update_interaction(...)`
  - `resolve_interaction(...)`
  - `emit_interaction_result(...)`
- `ChannelRuntime` is the core channel bus for endpoint instances:
  - it lists endpoints
  - polls each endpoint mailbox
  - flushes each endpoint outbox
- `ChannelEndpointProviderManager` is the parent management/introspection router:
  - it registers channel providers
  - maps persisted endpoint type keys (`channel_kind`) to providers
  - dispatches attach/detach/restart and endpoint introspection by `endpoint_id`
  - rescans providers without treating `channel_kind` as an LLM-facing concept
- runtime-root channel providers live under:
  - `runtime_root/channel/providers/<provider_id>/provider.toml`
  - the manifest points at provider-owned Python code, usually `entrypoint = "runtime.py"`
  - rescan loads those providers and registers them with `ChannelEndpointProviderManager`
- channel providers own their concrete lifecycle and endpoint introspection:
  - endpoint construction/deserialization
  - attach/detach/restart implementation
  - auth/health/backlog/inspect payloads
  - channel-specific interaction rendering and callback parsing
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
- channel providers are registered through `ChannelEndpointProviderManager`, so
  built-in and community plugins can contribute new endpoint type providers
  without changing the channel core
- no channel operation surface is exposed to `Pal/LLM`
- secrets/tokens are write-only; introspection never returns them

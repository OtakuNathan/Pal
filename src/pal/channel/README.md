# channel

Owns:
- endpoint runtime instances and their parent management layer
- inbound mailbox and outbound outbox runtime
- normalize, reply routing, and response handles
- active text delivery resolved by configured endpoint id
- adapter registry for legacy/transport compatibility
- provider manager for runtime-root channel providers
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
- `ChannelMessageReceipt`
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
- active delivery is a separate contract from replying to a turn:
  - LLM-facing: `channel_send_message(channel_id, message)`
  - runtime: resolve one attached, enabled endpoint by `channel_id`
  - endpoint: `send_message(message)` using only its persisted binding
  - provider-specific recipients are never accepted from the LLM
  - the default endpoint implementation reuses the reply outbox only when
    `derive_default_reply_target()` yields one unambiguous bound recipient
  - reply-only endpoints (including the local socket endpoint) reject active send
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
  - rescan loads that directory as a Python package through `importlib`, so
    provider-owned modules may use relative imports without entering Pal's wheel
  - rescan registers the resulting provider with `ChannelEndpointProviderManager`
- provider-owned mutable state lives under:
  - `runtime_root/data/channel/<endpoint_id>/`
  - providers choose their private representation inside that directory
  - provider state is not part of Pal's central channel binding schema
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
- channel root and the recovery socket endpoint are core runtime components and
  are not hot-reloaded; every other provider/endpoint implementation is a
  runtime-root hot-reload boundary
- detachable providers are not imported from `site-packages`; deployment,
  replacement, and removal happen entirely within the selected runtime root
- only channel-neutral operations are exposed to `Pal/LLM`; provider-specific
  target addressing stays private to the endpoint
- secrets/tokens are write-only; introspection never returns them

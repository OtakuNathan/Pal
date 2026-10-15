# channel

Owns:
- endpoint runtime instances and their parent management layer
- inbound mailbox and outbound outbox runtime
- normalize, reply routing, and response handles
- active text delivery resolved by configured endpoint id
- adapter registry for legacy/transport compatibility
- resident provider registry and endpoint management; PluginHost owns provider code generations
- stable per-endpoint hubs across transport attach/detach
- delivery diagnostics for queued replies and hub-buffer pressure
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
- `ChannelMessage` (`text` fallback plus optional provider presentation tag/payload)
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
- every endpoint receives the same provider-neutral stream events. TTY and the
  recovery socket may render deltas immediately; Telegram accumulates them,
  keeps tool rounds silent, and sends only the terminal answer
- reply completion for a turn means "accepted by channel outbox"
- tagged messages remain ordinary user-visible replies: providers may enhance
  known tags, while unknown tags must deliver the complete fallback text
- actual delivery success/failure is emitted later as channel-side diagnostics
- concrete endpoint implementations inherit `ChannelEndpointQueueBase` and fill in:
  - `normalize_raw(...)`
  - `send_reply(...)`
  - `inspect_health(...)`
  - `inspect_auth_state(...)`
- active delivery is a separate contract from replying to a turn:
  - LLM-facing: `send_channel_message(channel_id, message)`
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
  - dispatches attach/detach/endpoint restart and introspection by `endpoint_id`
  - accepts channel extensions installed by PluginHost
  - keeps endpoint identity and buffers when a plugin generation is detached
- runtime-root channel providers live under:
  - `runtime_root/channel/providers/<provider_id>/provider.toml`
  - the manifest points at provider-owned Python code, usually `entrypoint = "runtime.py"`
  - PluginHost adapts this legacy manifest in place; attach validates the provider
    contract and establishes endpoint hubs. New plugins declare endpoint types
    in plugin.toml so discovery can establish hubs without loading code.
  - build code may register reverse-order cleanup callbacks; optional provider
    `attach`/`detach` hooks share the loaded-code RAII lifetime
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
  cannot be detached
- `manage_channel_endpoint(operation=restart)` refreshes one endpoint connection without evicting
  provider modules; `manage_plugin(operation="reload")` replaces the provider generation and restores
  eligible endpoints; `rescan_plugins` discovers metadata and physical removals.
  Use `manage_plugin(operation="attach")` after discovering a newly installed inactive plugin.
- `manage_channel_endpoint(operation=attach)` restores an explicitly detached endpoint. Repeating
  attach on a stable attached transport preserves that instance and reports
  `changed=false` and `transport_restarted=false`; it does not prove connection
  health. Conditional health/restart affordances guide connection repair.
  `manage_channel_endpoint(operation=restart)` replaces the transport when an attached connection
  has failed. Core endpoint reload uses restart, while core attach preserves it.
- EndpointHub is the internal registry and comes early/leaves late. It owns
  endpoint identity and ordered backpressure while transports and code are
  absent. The LLM execution registry comes late/leaves early: detach withdraws
  capability first; attach publishes only after transport startup and backlog
  drain. Physical removal reroutes remaining backlog to the recovery socket and
  deletes the hub. Lifecycle gaps are retryable and do not trigger Safe Mode
- `lifecycle.py` is the executable EndpointHub reducer source of truth. The
  reducer owns lifecycle state, physical/transport presence, capability
  publication, publication intent, and buffer guards as one snapshot. Its
  complete generated relation and the origin/socket protocol model live in
  `spec/channel`; run `scripts/check_channel_tla.sh tla2tools.jar` after changing
  lifecycle or fallback semantics
- detachable providers are not imported from `site-packages`; deployment,
  replacement, and removal happen entirely within the selected runtime root
- only channel-neutral operations are exposed to `Pal/LLM`; provider-specific
  target addressing stays private to the endpoint
- secrets/tokens are write-only; introspection never returns them

Channel plugin contract:
- New packages use a normal `plugin.toml` with `lifecycle_protocol = "raii.v1"`,
  `requires_ports = ["channel:provider_manager"]`, and a `[channel]` table with
  `endpoint_types = ["my_protocol"]`.
- `start(scope)` returns a `ModuleHandle` with
  `channel_extension=ChannelExtension(provider=provider)` from `pal.channel.plugin`.
  The provider id equals the owning plugin id. Its endpoint types must match the
  manifest. PluginHost installs the contribution and registers generation cleanup.
- The provider implements the existing ChannelProvider contract. Optional
  attach/detach hooks and build cleanup retain their retryable ownership.
- Endpoint detach does not unload a plugin. Plugin detach stops its transports
  while retaining hubs and endpoint binding/attach intent; plugin attach restores
  eligible endpoints. Disabling the plugin cannot be bypassed by endpoint attach.
- A removed package is detached before hub removal and recovery rerouting.
- Legacy `provider.toml` enabled state initializes plugin settings; later
  enable/disable operations use PluginHost. No runtime files are moved.
- Python compatibility methods `reload_provider` and `rescan_providers` delegate
  to PluginHost when composed; separate model-facing provider lifecycle tools
  are no longer published.

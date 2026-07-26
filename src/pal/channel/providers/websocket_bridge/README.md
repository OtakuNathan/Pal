# Pal-to-Pal LAN WebSocket Bridge Provider

This directory ships the **runtime-root channel provider** that exposes the
Pal-to-Pal LAN WebSocket bridge as a single **transport-lifecycle** channel
endpoint through `ChannelEndpointProviderManager`.

> The bridge is **intentionally not a parallel WebSocket channel.** It reuses the
> existing socket channel for **all** message semantics. The only thing this
> provider owns is the WebSocket **sidecar subprocess** lifecycle plus the
> provider-owned introspection surface.

## Files

| File            | Role                                                                                |
| --------------- | ----------------------------------------------------------------------------------- |
| `provider.toml` | Runtime-root provider manifest (discovered by `ChannelEndpointProviderManager`).    |
| `runtime.py`    | Provider + endpoint implementation loaded via `build_channel_provider`.             |
| `protocol.py`   | Frozen bridge adaptation contract (read-only boundary-rejection rules).             |
| `sidecar.py`    | Public declarations for the WebSocket sidecar process (owned separately).           |

## How it fits

* `ChannelEndpointProviderManager` scans `<runtime_root>/channel/providers/`,
  reads `provider.toml`, and calls `build_channel_provider(context)`, which
  returns a `WebSocketBridgeProvider`.
* `attach_endpoint` builds a `WebSocketBridgeEndpoint` from the endpoint's
  `channel_endpoints` row and registers it with `ChannelRuntime`.
* When the runtime starts, the endpoint's `start_async` **spawns and supervises**
  the WebSocket sidecar as a subprocess. The sidecar bridges WebSocket text
  frames into the existing socket channel and back — it is the sole WebSocket
  wire implementation (`websockets` library).
* `stop_async` cleanly shuts the sidecar down (manager-socket shutdown RPC, then
  process termination) and releases the manager socket.

### Manager socket + subprocess conventions

The provider owns the subprocess handle and supervises it over the repo
`SidecarEndpoint` manager socket located at
`<runtime_root>/data/websocket_bridge/manager.sock` (the established repo
sidecar convention, mirroring the LSP manager). It:

* spawns the sidecar with `python_subprocess_env()` plus this provider directory
  on `PYTHONPATH`,
* hands the sidecar its serialized `SidecarConfig` via the
  `PAL_WEBSOCKET_BRIDGE_CONFIG` environment variable (the child reconstructs the
  config and runs the declared `serve` entrypoint),
* probes `health` and `shutdown` over the manager socket,
* accepts the endpoint-private `send_message` RPC used by
  `channel_send_message(channel_id, message)`,
* force-terminates the process group if a clean shutdown does not return in time.

The sidecar process **exclusively owns** its WebSocket connections and its socket
channel client sessions. The provider owns neither.

## Message path

The provider endpoint performs **no direct** message ingress and adds no new
wire-level channel semantics:

* `normalize_raw` returns an empty dict for every inbound payload.
* `send_reply` is a no-op — replies flow back over the existing socket channel.
* `send_message(message)` asks the sidecar to send an existing socket-protocol
  `user_message` frame to its single configured peer and waits for the matching
  final response.

Inbound WebSocket messages are delivered by the sidecar into the existing socket
channel user-message path. WebSocket response frames are correlated by the
existing `request_id`; reasoning, tool progress, and text from intermediate
tool-call rounds are filtered rather than redelivered as messages. The manager
RPC is private sidecar IPC and does not add a WebSocket wire protocol.

## Health

`inspect_health` reflects the live sidecar state:

| Field             | Source                                                              |
| ----------------- | ------------------------------------------------------------------- |
| `process_running` | The owned subprocess is alive.                                      |
| `listener_bound`  | The sidecar reports its WebSocket listener is bound.                |
| `connected_peers` | Number of currently connected peers (from the sidecar health RPC).  |
| `last_error`      | The most recent startup/probe error (empty when healthy).           |
| `healthy`         | `process_running` **and** `listener_bound`.                         |

Sidecar startup or health failure marks the endpoint health as **unhealthy**
rather than raising — the runtime keeps running and surfaces the failure via
introspection.

## Usage

1. Apply the schema/row patch in `docs/websocket_bridge_endpoint_row.sql` (with
   explicit user approval) so a `channel_endpoints` row of `channel_kind =
   'websocket_bridge'` exists.
2. Attach the endpoint through the provider manager (attach → spawns the sidecar
   on runtime start; detach → terminates it). The lifecycle is reversible.

Binding metadata on the row carries the transport configuration, e.g.:

```json
{
  "bind_host": "0.0.0.0",
  "bind_port": 8765,
  "peer_url": "ws://peer-host:8765",
  "reconnect_initial_delay_seconds": 1.0,
  "reconnect_max_delay_seconds": 30.0,
  "message_timeout_seconds": 3000.0
}
```

## Non-goals

This is a trusted-LAN, message-only bridge. The following are explicitly **out of
scope** for both the provider and the sidecar:

* **No public-internet exposure.** The bridge targets a trusted LAN only. It must
  not be exposed to the public internet; binding should be constrained to the LAN,
  never to the open internet.
* **No TLS termination.** The bridge provides no TLS — it does not terminate or
  originate TLS connections. It is intended for trusted-LAN use where TLS is not
  required, and adds no TLS layer of its own.
* **No autodiscovery.** There is no service discovery, mDNS, or peer auto-discovery.
  Peers and bind addresses are configured explicitly (binding metadata on the
  endpoint row); the bridge does not autodiscover peers.
* **No clustering.** The bridge is point-to-point between two Pal runtimes on a
  LAN. It does not form a cluster, coordinate a cluster, or scale horizontally
  across nodes.
* **No fanout.** The bridge is one-to-one. It does not fanout, broadcast, or
  replicate messages to multiple peers — each bridge endpoint serves a single
  peer connection.
* **No remote control of another Pal runtime.** The bridge carries user messages
  only. It provides no remote control, remote management, or orchestration of
  another Pal runtime. A peer Pal runtime is never remotely driven through the
  bridge.

The following architectural non-goals also apply:

* **Not a new channel.** This is a transport-lifecycle adapter over the existing
  socket channel — it does not introduce message kinds, envelopes, or semantics.
* **No message ingress in the provider.** All ingress is owned by the sidecar and
  delivered through the socket channel.
* **No parallel reply semantics.** Replies retain the existing socket response
  shapes and request ids; the sidecar only correlates them for the active sender.
* **No schema mutation without approval.** Production `channel_endpoints` row or
  schema changes are prepared as a patch (`docs/`) and applied only with explicit
  user approval.

# Pal-to-Pal LAN WebSocket Bridge Provider

This directory is the source of the **runtime-root channel provider** that exposes
the Pal-to-Pal LAN WebSocket bridge as a point-to-point channel endpoint through
`ChannelEndpointProviderManager`.

The endpoint owns both the WebSocket sidecar lifecycle and a dedicated local
channel socket at `<runtime_root>/data/channel/<endpoint_id>/channel.sock`. Peer
traffic never enters the TTY socket at `<runtime_root>/pal.sock`.

## Files

| File            | Role                                                                                |
| --------------- | ----------------------------------------------------------------------------------- |
| `provider.toml` | Runtime-root provider manifest (discovered by `ChannelEndpointProviderManager`).    |
| `runtime.py`    | Provider + endpoint implementation loaded via `build_channel_provider`.             |
| `protocol.py`   | Frozen bridge adaptation contract (read-only boundary-rejection rules).             |
| `sidecar.py`    | Public declarations for the WebSocket sidecar process (owned separately).           |

## How it fits

* Packaging deploys this directory to `<runtime_root>/channel/providers/`, and
  `ChannelEndpointProviderManager` scans that runtime-owned location,
  reads `provider.toml`, and calls `build_channel_provider(context)`, which
  returns a `WebSocketBridgeProvider`.
* `attach_endpoint` builds a `WebSocketBridgeEndpoint` from the endpoint's
  `channel_endpoints` row and registers it with `ChannelRuntime`.
* When the runtime starts, the endpoint's `start_async` binds its private channel
  socket and **spawns and supervises** the WebSocket sidecar as a subprocess.
  The sidecar is the sole WebSocket wire implementation (`websockets` library).
* `stop_async` cleanly shuts the sidecar down (manager-socket shutdown RPC, then
  process termination) and releases both provider-owned sockets.

### Manager socket + subprocess conventions

The provider owns the subprocess handle and supervises it over the repo
`SidecarEndpoint` manager socket located at
`<runtime_root>/data/channel/<endpoint_id>/manager.sock` (the provider-owned
sidecar convention, mirroring the LSP manager). It:

* spawns the sidecar with `python_subprocess_env()` plus this provider directory
  on `PYTHONPATH`,
* hands the sidecar its serialized `SidecarConfig` via the
  `PAL_WEBSOCKET_BRIDGE_CONFIG` environment variable (the child reconstructs the
  config and runs the declared `serve` entrypoint),
* probes `health` and `shutdown` over the manager socket,
* accepts the endpoint-private fire-and-forget `send_message` RPC used by
  `channel_send_message(channel_id, message)`,
* force-terminates the process group if a clean shutdown does not return in time.

The sidecar process exclusively owns its WebSocket connections and opens
short-lived clients of the provider's dedicated local channel socket.

## Message path

The provider reuses the ordinary socket framing implementation on its private
socket while keeping peer semantics endpoint-owned:

* `normalize_raw` appends the local endpoint identity to inbound text as
  `--<endpoint_id>`. The suffix is mechanical provenance, not model-authored
  content.
* `send_reply` streams this turn's normal final response to the waiting sidecar
  client. Reasoning, tool calls, and intermediate model rounds are not sent to
  the peer.
* `send_message(message)` starts a new peer exchange and returns as soon as the
  connected WebSocket accepts the root frame.
* Calling `channel_send_message` for the same endpoint while handling its current
  peer turn is forbidden. The model replies with its normal final response.

Every peer frame carries an exchange UUID and one-based message count. Messages
1 through 8 may enter Pal; an attempted ninth message is dropped before ingress.
An exact entire final of `[[peer_end]]` is also dropped before ingress. Sentinel,
limit, invalid context, delivery error, or disconnect terminates the exchange
and clears process-local exchange state. The next explicit
`channel_send_message` begins a fresh exchange at count 1.

Legacy socket response frames received from a peer are discarded. They are never
reinterpreted as new user input.

## Health

`inspect_health` reflects the live sidecar state:

| Field             | Source                                                              |
| ----------------- | ------------------------------------------------------------------- |
| `process_running` | The owned subprocess is alive.                                      |
| `listener_bound`  | The sidecar reports its WebSocket listener is bound.                |
| `connected_peers` | Number of currently connected peers (from the sidecar health RPC).  |
| `last_error`      | The most recent startup/probe error (empty when healthy).           |
| `channel_socket_bound` | The provider-owned local channel socket is accepting clients. |
| `healthy`         | Process, WebSocket listener, and private channel socket are live. |

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
  "reconnect_max_delay_seconds": 30.0
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

* **No new core event kind.** The dedicated endpoint still emits the normal
  channel envelope and L1 user-message path.
* **No TTY coupling.** The WebSocket bridge must not open, depend on, or inject
  peer traffic through `<runtime_root>/pal.sock`.
* **No response-frame reinjection.** Only peer `user_message` frames can become
  local input; response frames are transport output and are discarded on input.
* **No schema mutation without approval.** Production `channel_endpoints` row or
  schema changes are prepared as a patch (`docs/`) and applied only with explicit
  user approval.

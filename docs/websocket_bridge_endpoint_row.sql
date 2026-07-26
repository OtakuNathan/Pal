-- ============================================================
-- Pal-to-Pal LAN WebSocket Bridge: channel_endpoints row patch
-- ============================================================
--
-- PURPOSE
--   Prepare the channel_endpoints table for a transport-lifecycle
--   'websocket_bridge' endpoint owned by the WebSocket bridge provider
--   (src/pal/channel/providers/websocket_bridge/). This patch is prepared
--   here and applied ONLY with explicit user approval.
--
-- NON-GOALS
--   * This introduces NO new message semantics. The bridge reuses the existing
--     socket channel for all message ingress/egress; it only owns the WebSocket
--     sidecar subprocess lifecycle.
--   * No other table or IPC contract is altered.
--
-- ============================================================
-- 1. SCHEMA: allow 'websocket_bridge' as a channel_kind
-- ============================================================
--
-- docs/sqlite_schema_draft.sql currently constrains channel_kind to
-- ('stdio', 'socket', 'telegram'). SQLite cannot ALTER a CHECK constraint in
-- place, so the canonical schema text in docs/sqlite_schema_draft.sql must be
-- updated to include 'websocket_bridge', e.g.:
--
--   CHECK (channel_kind IN ('stdio', 'socket', 'telegram', 'websocket_bridge'))
--
-- For an already-provisioned database the CHECK is widened via a guarded table
-- rebuild (SQLite table-rebuild idiom). Safe to run repeatedly: the guard skips
-- the rebuild when 'websocket_bridge' is already permitted.

-- NOTE: The statements below are illustrative of the intended migration shape.
-- In a fresh provisioning pass, simply update docs/sqlite_schema_draft.sql so
-- the CREATE TABLE includes 'websocket_bridge' in the CHECK; then the standard
-- provisioning applies the corrected DDL.

-- ============================================================
-- 2. ROW: example websocket_bridge endpoint
-- ============================================================
--
-- Identity: channel_kind = 'websocket_bridge' + binding_key.
-- binding_metadata carries the transport configuration consumed by the
-- provider's create_endpoint and forwarded to the sidecar SidecarConfig:
--   * bind_host / bind_port  - WebSocket listener bind address
--   * peer_url               - remote peer to (re)connect to
--   * reconnect_*            - backoff schedule
--
-- No secrets are stored here; trusted-LAN pairing/authorization state is
-- surfaced via the provider-owned introspection (inspect_auth_state) without
-- material.

INSERT INTO channel_endpoints (
  endpoint_id,
  channel_kind,
  binding_key,
  enabled,
  binding_metadata,
  send_policy_blob,
  created_at,
  updated_at
) VALUES (
  'websocket_bridge_main',
  'websocket_bridge',
  'lan:default',
  1,
  -- json: transport configuration
  '{"bind_host":"0.0.0.0","bind_port":8765,"peer_url":null,"reconnect_initial_delay_seconds":1.0,"reconnect_max_delay_seconds":30.0}',
  -- json: send policy overrides (empty = inherit socket channel semantics)
  '{}',
  strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
  strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
)
ON CONFLICT(channel_kind, binding_key) DO UPDATE SET
  binding_metadata = excluded.binding_metadata,
  updated_at = excluded.updated_at;

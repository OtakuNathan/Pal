PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

BEGIN;

-- ============================================================
-- Pal Architecture V1 — SQLite Schema
--
-- Single-agent, single-user, single-governance-queue.
-- user_pal_id / user_id removed from per-domain tables.
--
-- Table ordering follows dependency graph:
--   identity → channel → llm → memory → tasking → proactive → diagnostics
-- ============================================================


-- ============================================================
-- IDENTITY & PERSONA
-- ============================================================

-- Single Pal instance persona.
-- Populated by setup wizard. Not overridable by skill.
CREATE TABLE IF NOT EXISTS pal_personas (
  persona_id   TEXT PRIMARY KEY,            -- singleton row, e.g. 'default'
  display_name TEXT NOT NULL,
  language     TEXT NOT NULL DEFAULT 'en',
  vibe         TEXT,                        -- personality vibe description
  tone         TEXT,                        -- communication tone
  style_notes   TEXT,                       -- free-form style preferences
  core_policy  TEXT,                        -- behavioral constraints (JSON array of rules)
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  CHECK (core_policy IS NULL OR json_valid(core_policy))
);

-- CHECK constraint on core_policy handled in table definition below

-- Structured user preferences.
CREATE TABLE IF NOT EXISTS user_preferences (
  preference_id TEXT PRIMARY KEY,           -- singleton row
  language_preference TEXT,
  style_preference    TEXT,
  timezone            TEXT,
  preferences_blob    TEXT,                 -- extensible key-value preferences
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  CHECK (preferences_blob IS NULL OR json_valid(preferences_blob))
);

-- ============================================================
-- CHANNEL ENDPOINTS
-- ============================================================

-- Replaces old channel_bindings + conversation_routes.
-- One row = one addressable channel endpoint (where messages come from / go to).
-- Identity: channel_kind + endpoint_id
CREATE TABLE IF NOT EXISTS channel_endpoints (
  endpoint_id         TEXT PRIMARY KEY,
  channel_kind        TEXT NOT NULL,            -- stdio | socket | telegram
  binding_key         TEXT NOT NULL,             -- e.g. chat_id, socket path
  enabled             INTEGER NOT NULL DEFAULT 1,

  -- EndpointConfig fields
  max_message_chars   INTEGER,                  -- send policy: single output limit
  preferred_parse_mode TEXT,                    -- e.g. MarkdownV2, plain
  segment_by_default  INTEGER NOT NULL DEFAULT 0,
  preserve_code_blocks INTEGER NOT NULL DEFAULT 1,
  supports_typing     INTEGER NOT NULL DEFAULT 0,
  supports_receipt_marker INTEGER NOT NULL DEFAULT 0,
  supports_message_edit INTEGER NOT NULL DEFAULT 0,

  -- Adapter-specific config
  binding_metadata    TEXT,                     -- JSON: bot token ref, chat id, etc.
  send_policy_blob    TEXT,                     -- JSON: extended send policy overrides

  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  detached_at         TEXT,

  CHECK (channel_kind IN ('stdio', 'socket', 'telegram')),
  CHECK (binding_metadata IS NULL OR json_valid(binding_metadata)),
  CHECK (send_policy_blob IS NULL OR json_valid(send_policy_blob))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_channel_endpoints_kind_key
ON channel_endpoints(channel_kind, binding_key);


-- ============================================================
-- LLM ENDPOINTS
-- ============================================================

-- Local model capability truth source.
-- Not relying on provider online discovery.
-- Priority drives fallback order.
-- Lower numbers are tried first. This is a routing priority, not a
-- quality weight. Keep values non-negative and small unless there is a
-- concrete migration reason to do otherwise.
CREATE TABLE IF NOT EXISTS llm_endpoints (
  endpoint_id       TEXT PRIMARY KEY,
  provider          TEXT NOT NULL,              -- openai | anthropic | custom
  model_id          TEXT NOT NULL,              -- exact model identifier
  display_name      TEXT,
  wire_shape        TEXT NOT NULL,              -- openai_completion | openai_response | anthropic_messages
  base_url          TEXT NOT NULL,

  -- Auth
  auth_kind         TEXT NOT NULL DEFAULT 'api_key_ref',  -- api_key_ref | oauth | local_provider_auth
  credential_ref    TEXT NOT NULL,              -- keychain ref or auth handle

  -- Capability metadata
  context_window      INTEGER,
  max_output_tokens   INTEGER,
  thinking_levels_blob TEXT NOT NULL DEFAULT '["off"]', -- JSON enum subset
  default_thinking_level TEXT,
  supports_tools      INTEGER NOT NULL DEFAULT 1,
  supports_streaming  INTEGER NOT NULL DEFAULT 1,
  supports_vision     INTEGER NOT NULL DEFAULT 0,
  input_modalities_blob  TEXT,                 -- JSON array
  output_modalities_blob TEXT,                 -- JSON array

  -- Routing
  priority          INTEGER NOT NULL DEFAULT 0, -- lower = higher priority; non-negative by policy
  enabled           INTEGER NOT NULL DEFAULT 1,

  -- Extended
  capabilities_blob TEXT,                      -- provider-specific metadata
  notes             TEXT,

  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,

  CHECK (wire_shape IN ('openai_completion', 'openai_response', 'anthropic_messages')),
  CHECK (auth_kind IN ('api_key_ref', 'oauth', 'local_provider_auth')),
  CHECK (capabilities_blob IS NULL OR json_valid(capabilities_blob)),
  CHECK (json_valid(thinking_levels_blob)),
  CHECK (input_modalities_blob IS NULL OR json_valid(input_modalities_blob)),
  CHECK (output_modalities_blob IS NULL OR json_valid(output_modalities_blob))
);

CREATE INDEX IF NOT EXISTS idx_llm_endpoints_priority
ON llm_endpoints(enabled, priority);

CREATE INDEX IF NOT EXISTS idx_llm_endpoints_capabilities
ON llm_endpoints(enabled, supports_tools, supports_streaming);


-- ============================================================
-- MEMORY — L3 TRUTH MODEL
-- ============================================================

-- Durable reusable facts.
-- task_id NULL = system scope; non-NULL = task scope.
CREATE TABLE IF NOT EXISTS memory_facts (
  fact_id            TEXT PRIMARY KEY,
  task_id            TEXT,                      -- NULL = system scope
  title              TEXT NOT NULL,
  summary            TEXT NOT NULL,
  search_text        TEXT NOT NULL,             -- normalized for lexical search
  canonical_key      TEXT,                      -- explicit fact identity for idempotent upsert
  dedupe_fingerprint TEXT,                      -- dedup on retire/archive

  payload_blob       TEXT,                      -- extensible fields, must NOT duplicate main columns

  lifecycle          TEXT NOT NULL DEFAULT 'active',
  use_count          INTEGER NOT NULL DEFAULT 0,
  last_used_at       TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,

  FOREIGN KEY (task_id) REFERENCES tasks(task_id),
  CHECK (lifecycle IN ('active', 'archived')),
  CHECK (payload_blob IS NULL OR json_valid(payload_blob))
);

CREATE INDEX IF NOT EXISTS idx_memory_facts_task
ON memory_facts(task_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_facts_canonical_key
ON memory_facts(canonical_key)
WHERE canonical_key IS NOT NULL AND canonical_key != '';

CREATE INDEX IF NOT EXISTS idx_memory_facts_dedupe
ON memory_facts(dedupe_fingerprint)
WHERE dedupe_fingerprint IS NOT NULL AND dedupe_fingerprint != '';


-- Durable reusable cases (STAR format).
-- task_id NULL = system scope; non-NULL = task scope.
CREATE TABLE IF NOT EXISTS memory_cases (
  case_id            TEXT PRIMARY KEY,
  task_id            TEXT,                      -- NULL = system scope
  title              TEXT NOT NULL,
  summary            TEXT NOT NULL,
  situation_text     TEXT NOT NULL,
  task_text          TEXT NOT NULL,
  action_text        TEXT NOT NULL,
  result_text        TEXT NOT NULL,
  search_text        TEXT NOT NULL,             -- aggregated for lexical search
  dedupe_fingerprint TEXT,

  payload_blob       TEXT,                      -- non-core extensions only

  lifecycle          TEXT NOT NULL DEFAULT 'active',
  use_count          INTEGER NOT NULL DEFAULT 0,
  last_used_at       TEXT,
  created_at         TEXT NOT NULL,
  updated_at         TEXT NOT NULL,

  FOREIGN KEY (task_id) REFERENCES tasks(task_id),
  CHECK (lifecycle IN ('active', 'archived')),
  CHECK (payload_blob IS NULL OR json_valid(payload_blob))
);

CREATE INDEX IF NOT EXISTS idx_memory_cases_task
ON memory_cases(task_id, updated_at DESC);

CREATE INDEX IF NOT EXISTS idx_memory_cases_dedupe
ON memory_cases(dedupe_fingerprint)
WHERE dedupe_fingerprint IS NOT NULL AND dedupe_fingerprint != '';


-- ============================================================
-- MEMORY — UNIFIED DOCUMENT PROJECTION
-- ============================================================

-- VIEW: heterogeneous L3 truth → homogeneous document for retrieval.
-- All recall/search/ranking operates on this, not directly on truth tables.
CREATE VIEW IF NOT EXISTS memory_document_projection AS
SELECT
  'fact:' || fact_id    AS document_id,
  'fact'                AS owner_kind,
  fact_id               AS owner_id,
  task_id,
  title,
  summary,
  search_text,
  lifecycle,
  use_count,
  last_used_at,
  created_at,
  updated_at
FROM memory_facts

UNION ALL

SELECT
  'case:' || case_id    AS document_id,
  'case'                AS owner_kind,
  case_id               AS owner_id,
  task_id,
  title,
  summary,
  search_text,
  lifecycle,
  use_count,
  last_used_at,
  created_at,
  updated_at
FROM memory_cases;


-- ============================================================
-- MEMORY — INDEXES
-- ============================================================

-- Unified FTS5 over document projection.
-- Weighted: title(10), summary(5), search_text(3).
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
  document_id,
  title,
  summary,
  search_text,
  content='memory_document_projection',
  content_rowid='rowid',
  tokenize='unicode61'
);

-- NOTE: FTS sync for views requires application-level triggers or
-- explicit reindex after mutations on memory_facts / memory_cases.
-- Recommended: use insert/delete/update triggers on both truth tables
-- that call INSERT INTO memories_fts ... VALUES ('delete', ...) / VALUES (new.*, ...).
-- Trigger implementation deferred to repository layer.

-- Consolidated topic index.
-- Replaces old tags + memory_tags + topic_tags_blob.
CREATE TABLE IF NOT EXISTS memory_topics (
  document_id      TEXT NOT NULL,
  topic            TEXT NOT NULL,
  normalized_topic TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  PRIMARY KEY (document_id, normalized_topic)
);

CREATE INDEX IF NOT EXISTS idx_memory_topics_lookup
ON memory_topics(normalized_topic, document_id);

-- Detachable derived embedding index.
-- Not truth source. Reindex on provider change.
CREATE TABLE IF NOT EXISTS memory_embeddings (
  embedding_id     TEXT PRIMARY KEY,
  document_id      TEXT NOT NULL,
  embedding_kind   TEXT NOT NULL,               -- primary | context | resolution
  model_name       TEXT NOT NULL,
  model_revision   TEXT,
  source_text_hash TEXT NOT NULL,
  embedding_blob   TEXT NOT NULL,               -- JSON float array
  embedding_norm   REAL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  UNIQUE (document_id, embedding_kind, model_name, model_revision)
);

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_document
ON memory_embeddings(document_id);

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_model
ON memory_embeddings(embedding_kind, model_name, model_revision);


-- ============================================================
-- TASKING
-- ============================================================

-- Tasking subsystem runtime state.
-- Owned by tasking plugin, not by Pal Core.
CREATE TABLE IF NOT EXISTS tasking_state (
  state_id              TEXT PRIMARY KEY,         -- singleton row, e.g. 'default'
  active_task_id        TEXT,
  active_work_order_refs TEXT NOT NULL DEFAULT '[]',
  updated_at            TEXT NOT NULL,
  FOREIGN KEY (active_task_id) REFERENCES tasks(task_id),
  CHECK (json_valid(active_work_order_refs))
);

-- Long-lived work object.
-- Removed: user_pal_id, in_channel_blob, out_channel_blob.
CREATE TABLE IF NOT EXISTS tasks (
  task_id                      TEXT PRIMARY KEY,
  title                        TEXT NOT NULL,
  goal                         TEXT NOT NULL,
  status                       TEXT NOT NULL DEFAULT 'active',
  current_state_summary        TEXT,
  current_progress_summary     TEXT,
  active_work_order_id         TEXT,
  default_worker_requirements  TEXT,           -- JSON
  active_branch                TEXT,
  active_artifact_ref          TEXT,
  recommended_model            TEXT,
  in_channel_id                TEXT,            -- FK to channel endpoint (where task was created)
  created_at                   TEXT NOT NULL,
  updated_at                   TEXT NOT NULL,
  archived_at                  TEXT,

  FOREIGN KEY (active_work_order_id) REFERENCES work_orders(work_order_id),
  FOREIGN KEY (in_channel_id) REFERENCES channel_endpoints(endpoint_id),
  CHECK (status IN ('active', 'working', 'inactive', 'archived')),
  CHECK (default_worker_requirements IS NULL OR json_valid(default_worker_requirements))
);

CREATE INDEX IF NOT EXISTS idx_tasks_by_active_work_order
ON tasks(active_work_order_id);

CREATE INDEX IF NOT EXISTS idx_tasks_status
ON tasks(status, updated_at DESC);

-- One concrete unit of work under a task.
CREATE TABLE IF NOT EXISTS work_orders (
  work_order_id         TEXT PRIMARY KEY,
  task_id               TEXT NOT NULL,
  origin_conversation_id TEXT,
  sub_title             TEXT NOT NULL,
  description           TEXT,
  goal                  TEXT NOT NULL,
  arch_draft            TEXT,
  dependency_blob       TEXT,                   -- JSON
  worker_requirements   TEXT,                   -- JSON
  recommended_model     TEXT,
  planner_model_hint    TEXT,
  executor_model_hint   TEXT,
  acceptance_criteria   TEXT NOT NULL,
  execution_plan_blob   TEXT,                   -- JSON
  active_step_id        TEXT,
  scope_blob            TEXT,                   -- JSON
  policy_blob           TEXT,                   -- JSON
  status                TEXT NOT NULL DEFAULT 'created',
  current_status_note   TEXT,
  iteration_budget      INTEGER NOT NULL DEFAULT 10,
  out_channel_id        TEXT,                   -- where to send results
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,
  done_at               TEXT,

  FOREIGN KEY (task_id) REFERENCES tasks(task_id),
  FOREIGN KEY (out_channel_id) REFERENCES channel_endpoints(endpoint_id),
  CHECK (status IN ('created', 'working', 'awaiting_approval', 'error', 'done')),
  CHECK (iteration_budget > 0),
  CHECK (dependency_blob IS NULL OR json_valid(dependency_blob)),
  CHECK (worker_requirements IS NULL OR json_valid(worker_requirements)),
  CHECK (execution_plan_blob IS NULL OR json_valid(execution_plan_blob)),
  CHECK (scope_blob IS NULL OR json_valid(scope_blob)),
  CHECK (policy_blob IS NULL OR json_valid(policy_blob))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_work_orders_one_active_per_task
ON work_orders(task_id)
WHERE status IN ('created', 'working', 'awaiting_approval');

CREATE INDEX IF NOT EXISTS idx_work_orders_status
ON work_orders(task_id, status, updated_at DESC);

-- Formal approval record.
-- Lifecycle: pending → approved/rejected/expired/cancelled → consumed.
CREATE TABLE IF NOT EXISTS approvals (
  approval_id           TEXT PRIMARY KEY,
  proposal_id           TEXT NOT NULL UNIQUE,
  task_id               TEXT NOT NULL,
  work_order_id         TEXT NOT NULL,
  proposal_snapshot     TEXT NOT NULL,           -- JSON snapshot of what's being approved
  target_digest         TEXT NOT NULL,           -- summary of target
  request_kind          TEXT,                    -- e.g. 'worker_action', 'destructive_op'
  action_summary        TEXT,
  reason                TEXT,
  fallback              TEXT,
  delivery_state        TEXT NOT NULL DEFAULT 'enveloped',
  decision              TEXT,
  decision_note         TEXT,
  status                TEXT NOT NULL DEFAULT 'pending',
  requested_at          TEXT NOT NULL,
  decided_at            TEXT,
  archived_at           TEXT,

  FOREIGN KEY (task_id) REFERENCES tasks(task_id),
  FOREIGN KEY (work_order_id) REFERENCES work_orders(work_order_id),
  CHECK (json_valid(proposal_snapshot)),
  CHECK (delivery_state IN ('enveloped', 'presented', 'decided', 'archived', 'consumed')),
  CHECK (decision IS NULL OR decision IN ('approved', 'rejected', 'expired', 'cancelled')),
  CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled', 'consumed'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_one_pending_per_work_order
ON approvals(work_order_id)
WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_approvals_queue
ON approvals(status, requested_at);

-- Resumable worker checkpoint.
-- Continuity binds here, not to worker process.
CREATE TABLE IF NOT EXISTS worker_checkpoints (
  checkpoint_id         TEXT PRIMARY KEY,
  task_id               TEXT NOT NULL,
  work_order_id         TEXT NOT NULL,
  summary               TEXT NOT NULL,
  progress_summary      TEXT,
  completed_steps_blob  TEXT,                    -- JSON array
  next_resume_point     TEXT,
  active_artifact_refs  TEXT,                    -- JSON array
  branch                TEXT,
  git_commit            TEXT,                    -- git commit hash
  verification_snapshot TEXT,                    -- JSON
  created_at            TEXT NOT NULL,

  FOREIGN KEY (task_id) REFERENCES tasks(task_id),
  FOREIGN KEY (work_order_id) REFERENCES work_orders(work_order_id),
  CHECK (completed_steps_blob IS NULL OR json_valid(completed_steps_blob)),
  CHECK (active_artifact_refs IS NULL OR json_valid(active_artifact_refs))
);

CREATE INDEX IF NOT EXISTS idx_worker_checkpoints_by_work_order
ON worker_checkpoints(work_order_id, created_at DESC);

-- Formal tasking ledger.
-- Records the full lifecycle of worker operations.
CREATE TABLE IF NOT EXISTS worker_ledger (
  ledger_id       TEXT PRIMARY KEY,
  task_id         TEXT NOT NULL,
  work_order_id   TEXT NOT NULL,
  entry_kind      TEXT NOT NULL,               -- accepted | progress | checkpoint | replaced | terminated | closed
  worker_ref      TEXT,                         -- worker process identifier
  summary         TEXT,
  detail_blob     TEXT,                         -- JSON: structured detail per entry_kind
  created_at      TEXT NOT NULL,

  FOREIGN KEY (task_id) REFERENCES tasks(task_id),
  FOREIGN KEY (work_order_id) REFERENCES work_orders(work_order_id),
  CHECK (entry_kind IN ('accepted', 'progress', 'checkpoint', 'replaced', 'terminated', 'closed')),
  CHECK (detail_blob IS NULL OR json_valid(detail_blob))
);

CREATE INDEX IF NOT EXISTS idx_worker_ledger_by_work_order
ON worker_ledger(work_order_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_worker_ledger_by_task
ON worker_ledger(task_id, created_at DESC);


-- ============================================================
-- PROACTIVE
-- ============================================================

CREATE TABLE IF NOT EXISTS proactive_definitions (
  proactive_id          TEXT PRIMARY KEY,
  goal                  TEXT NOT NULL,
  method                TEXT NOT NULL DEFAULT '',
  skill_refs_blob       TEXT NOT NULL DEFAULT '[]',
  out_channel_id        TEXT,
  schedule_blob         TEXT NOT NULL DEFAULT '{}',
  out_reply_target_blob TEXT NOT NULL DEFAULT '{}',
  enabled               INTEGER NOT NULL DEFAULT 1,
  next_due_at_utc       TEXT,
  last_run_at_utc       TEXT,
  created_at            TEXT NOT NULL,
  updated_at            TEXT NOT NULL,

  CHECK (json_valid(skill_refs_blob)),
  CHECK (json_valid(schedule_blob)),
  CHECK (json_valid(out_reply_target_blob))
);

CREATE INDEX IF NOT EXISTS idx_proactive_definitions_due
ON proactive_definitions(enabled, next_due_at_utc);

CREATE TABLE IF NOT EXISTS proactive_runs (
  proactive_run_id TEXT PRIMARY KEY,
  proactive_id     TEXT NOT NULL,
  trigger_kind     TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'running',
  trigger_metadata TEXT NOT NULL DEFAULT '{}',
  turn_id          TEXT,
  output_summary   TEXT,
  error_text       TEXT,
  started_at       TEXT NOT NULL,
  completed_at     TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,

  CHECK (json_valid(trigger_metadata))
);

CREATE INDEX IF NOT EXISTS idx_proactive_runs_by_task
ON proactive_runs(proactive_id, started_at DESC);


-- ============================================================
-- REMINDERS
-- ============================================================

CREATE TABLE IF NOT EXISTS pal_reminders (
  reminder_id    TEXT PRIMARY KEY,
  title          TEXT NOT NULL,
  body           TEXT,
  due_at_utc     TEXT NOT NULL,
  origin_timezone TEXT,
  in_channel_id  TEXT NOT NULL,
  out_channel_id TEXT NOT NULL,
  status         TEXT NOT NULL DEFAULT 'scheduled',
  metadata_blob  TEXT,
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  sent_at        TEXT,
  archived_at    TEXT,

  FOREIGN KEY (in_channel_id) REFERENCES channel_endpoints(endpoint_id),
  FOREIGN KEY (out_channel_id) REFERENCES channel_endpoints(endpoint_id),
  CHECK (status IN ('scheduled', 'sent', 'cancelled', 'archived')),
  CHECK (metadata_blob IS NULL OR json_valid(metadata_blob))
);

CREATE INDEX IF NOT EXISTS idx_pal_reminders_due
ON pal_reminders(status, due_at_utc);


-- ============================================================
-- MCP SERVERS
-- ============================================================

CREATE TABLE IF NOT EXISTS mcp_servers (
  server_id                TEXT PRIMARY KEY,
  name                     TEXT NOT NULL,
  transport                TEXT NOT NULL,
  command_or_url           TEXT NOT NULL,
  args_blob                TEXT,
  env_blob                 TEXT,
  headers_blob             TEXT,
  auth_ref                 TEXT,
  enabled                  INTEGER NOT NULL DEFAULT 1,
  startup_timeout_seconds  REAL NOT NULL DEFAULT 15,
  tool_call_timeout_seconds REAL NOT NULL DEFAULT 30,
  server_metadata_blob     TEXT,
  last_loaded_at           TEXT,
  last_error               TEXT,
  created_at               TEXT NOT NULL,
  updated_at               TEXT NOT NULL,
  CHECK (transport IN ('stdio', 'streamable_http')),
  CHECK (args_blob IS NULL OR json_valid(args_blob)),
  CHECK (env_blob IS NULL OR json_valid(env_blob)),
  CHECK (headers_blob IS NULL OR json_valid(headers_blob)),
  CHECK (server_metadata_blob IS NULL OR json_valid(server_metadata_blob))
);


-- ============================================================
-- DIAGNOSTICS — STRUCTURED DEVELOPER REPORTS
-- ============================================================

-- Failure escalation reports.
-- Per pal_failure_reporting_contract.md.
-- Structured object, not natural language complaint.
CREATE TABLE IF NOT EXISTS developer_reports (
  report_id              TEXT PRIMARY KEY,
  subsystem              TEXT NOT NULL,            -- channel | memory | execution | control | tasking | proactive | plugin | llm
  component              TEXT,                     -- finer-grained: provider id, plugin id, etc.
  severity               TEXT NOT NULL,            -- low | medium | high | critical
  failure_kind           TEXT NOT NULL,            -- provider_failure | routing_failure | schema_failure | delivery_failure | maintenance_failure
  why_blocked            TEXT NOT NULL,            -- one-line: why this flow didn't complete
  current_blocker        TEXT NOT NULL,            -- minimal real blocker
  impact                 TEXT NOT NULL,            -- scope of impact
  attempted_actions_blob TEXT NOT NULL,            -- JSON array of attempted actions
  evidence_blob          TEXT NOT NULL,            -- JSON: stack traces, state snapshots, health reports
  documents_checked_blob TEXT NOT NULL,            -- JSON: configs, logs, objects inspected
  possible_solutions_blob TEXT NOT NULL,           -- JSON array of candidate next steps
  safe_to_retry          INTEGER NOT NULL DEFAULT 0,
  requires_developer_action INTEGER NOT NULL DEFAULT 1,
  recommended_next_step  TEXT,
  related_ids            TEXT,                     -- JSON: {task_id, work_order_id, proactive_id, ...}
  status                 TEXT NOT NULL DEFAULT 'open',
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL,
  resolved_at            TEXT,

  CHECK (subsystem IN ('channel', 'memory', 'execution', 'control', 'tasking', 'proactive', 'plugin', 'llm', 'core')),
  CHECK (severity IN ('low', 'medium', 'high', 'critical')),
  CHECK (status IN ('open', 'investigating', 'escalated', 'resolved', 'wontfix')),
  CHECK (json_valid(attempted_actions_blob)),
  CHECK (json_valid(evidence_blob)),
  CHECK (json_valid(documents_checked_blob)),
  CHECK (json_valid(possible_solutions_blob)),
  CHECK (related_ids IS NULL OR json_valid(related_ids))
);

CREATE INDEX IF NOT EXISTS idx_developer_reports_status
ON developer_reports(status, severity, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_developer_reports_subsystem
ON developer_reports(subsystem, status, created_at DESC);


-- ============================================================
-- ARTIFACTS
-- ============================================================

-- Artifact index; content stays in repo/workspace.
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id      TEXT PRIMARY KEY,
  work_order_id    TEXT,
  source_message_id TEXT,
  kind             TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'active',
  uri_or_ref       TEXT NOT NULL,
  summary          TEXT,
  metadata_blob    TEXT,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  FOREIGN KEY (work_order_id) REFERENCES work_orders(work_order_id),
  CHECK (status IN ('active', 'archived')),
  CHECK (metadata_blob IS NULL OR json_valid(metadata_blob))
);

CREATE INDEX IF NOT EXISTS idx_artifacts_by_work_order
ON artifacts(work_order_id, status, created_at DESC);


COMMIT;

-- ============================================================
-- APPENDIX: MIGRATION NOTES (v2 → v3)
--
-- DROPPED tables:
--   users, user_pals          → single-pal assumption, pal_personas only
--   channel_bindings          → channel_endpoints
--   conversation_routes       → channel_endpoints + runtime state
--   conversation_turns        → L1 (RAM-only)
--   conversation_summaries    → L1 (RAM-only)
--   pal_memories              → memory_facts + memory_cases
--   tags                      → memory_topics
--   memory_tags               → memory_topics
--   tool_results              → L1 (RAM-only)
--   memory_commit_markers     → L2 (RAM-only)
--   cached_queries            → removed
--   worker_profiles           → removed (not in contract)
--   user_profiles             → user_preferences
--   diagnostic_reports        → developer_reports (upgraded)
--
-- DROPPED columns:
--   user_pal_id / user_id     → single-pal, no multitenancy partition
--   fact_kind                 → memory contract no longer uses fact sub-kinds
--   prompt_pin                → top_of_mind is LRU view, not persistent flag
--   topic_tags_blob           → memory_topics table
--   confirmed_count           → not in minimum baseline
--   superseded_by             → not in minimum baseline
--   in_channel_blob           → in_channel_id FK to channel_endpoints
--   out_channel_blob          → out_channel_id FK to channel_endpoints
--
-- NEW tables:
--   memory_document_projection (VIEW) → unified retrieval layer
--   tasking_state               → tasking subsystem runtime state
--   worker_ledger               → formal tasking accounting
--   developer_reports           → structured failure escalation
--
-- ============================================================

# memory

Owns:
- `/memory_review` parsing, review decisions and presentation; replies use Core's control port
- L3 provider publication and selection
- compact capture, archive staging and candidate installation
- borrowed closed L1 prefixes with private transient tails
- `L1/L2/L3` memory semantics
- runtime-owned `L1` and `L2`, with encrypted resident baseline, incremental
  admission/round journal and a full clean-shutdown checkpoint
- durable `L3` provider contract; the SQLite-vec implementation is a plugin
- `pal.memory.persistence`: resident recovery and the separate searchable dialogue archive
- memory pack projection
- shared recall, L2 heat, and prompt projection for every agent host
- atomic replacement and rollback of an already validated compact summary

Does not own:
- channel state
- execution policy
- worker lifecycle
- service lifecycle
- compaction prompts, schemas, retry policy, or host
  semantic preferences

Exposes:
- `MemoryQuery`
- `MemoryPack`
- `MemoryService`
- `MemoryCompactResult`
- `L3ProviderPort`

`MemoryCompactRequest` is a retained tombstone for the retired whole-source
compaction interface. Current compaction captures LEFT history through
`HistoryRoot` and preserves RIGHT history.

Interaction rule:
- Pal and Bunshin instantiate the same `MemoryService` behavior and
  `MemoryPromptFragmentProvider`
- L1 is the complete working-set truth for the current logical process.
  Recall and L2 heat may be projected into prompts, but compaction never treats
  those projections as additional source records
- L1 accepts only closed tool protocol: every assistant tool-call batch must
  contain exactly its matching results in the same transcript. Orphan,
  incomplete, and late unmatched results are rejected before they enter L1
- L1 preserves accepted reasoning and native provider continuation in IR through
  turn closure and restart. Recovery repairs unmatched/duplicate tool protocol
  and incomplete streams without inventing outcomes. Explicit LEFT compaction
  replaces captured history with a text seed or an opaque native window. Native
  capture may include a completed RIGHT prefix without moving the live cut before
  commit; the uncaptured RIGHT suffix remains intact.
- Streaming assistant messages remain response drafts until a terminal response
  is accepted. Interrupt/abort and restart recovery move an unfinished response
  intact into private `discarded_assistant_drafts` diagnostics; they never mark
  its native replay complete. Previously accepted rounds and tool results stay
  verbatim. Recovery also recognizes the old interrupted, unsigned reasoning-only
  draft with an explicit empty item-commit ledger. Removing a recovered draft
  from LEFT translates the cut by message identity and advances its generation;
  unchanged prefixes retain their original cut identity.
- Memory exposes one runtime-state port for L1, L2, top-of-mind, and heat.
  Core orchestrates snapshot/restore/reset; Memory never reaches into Execution
- Resident recovery saves accepted L submissions and completed rounds through
  an encrypted baseline/journal. SIGINT/SIGTERM quiesces and writes a full atomic
  checkpoint without a model call. Startup restores working state before
  endpoints start; unfinished execution becomes interrupted context and is not
  replayed. This is boundary-driven persistence, not timer-driven compaction
- Startup accepts older schemas only when every state port declares them readable
  and the saved contract hash matches the recorded module schemas and resident
  identity. Successful restore rebases to current schemas before normal writes;
  a failed restore preserves the recovery point and blocks compact explicitly.
- Compact/reset archive only dialogue removed from L1 into credential-redacted
  daily JSONL under `data/conversation-history/`, by message date in Pal's timezone.
  Startup, shutdown and midnight do not archive active working dialogue.
  Searchable text excludes reasoning/native replay and does not replace recovery
- Core runs compaction and host policies render its checkpoint before storage;
  `MemoryService` performs the L1 replacement, dependent L2 cleanup, and
  execution-context projection inside one rollback boundary
- a storage or dependent-projection failure restores the original L1, L2,
  top-of-mind, and heat state

Dreaming enters maintenance silently after idle admission. Failures retain the
existing run report and private diagnostic log, then request an idempotent silent
once proactive handoff through Core. Notification transport is not a precondition
for sleep. The handoff uses existing proactive persistence and shared L1; see
[Dreaming](../../../docs/pal_memory_dreaming.md).

Dreaming is a first-party optional plugin dependent on `sqlite_vec_l3`. PluginHost
owns startup, shutdown and provider-generation replacement; Bootstrap does not
probe backend internals. Dreaming owns its cron timer and failure handoff prompt,
while Core supplies generic timers and maintenance admission. Memory backend
configuration operations are registered by Memory itself through the common RPC
operation registry.

Backend changes use Memory-owned generation migration and catalog CAS, with
transactional change capture, a final write fence, stable references and deferred
indexing. Provider identities are qualified by the L3 domain and validated before
module publication. Plugin cleanup preserves the committed selection. Dreaming and
backend migration share `GenerationMaintenance`, the production generation reducer,
and the single `SwapStore.publish` transaction. Semantic review may transform a
candidate; pure backend changes preserve canonical content. Backend changes notify
the last user route before sleep, await delivery, and fail before publication if
that delivery fails. The CLI announces sleep before requesting the change. Scheduled
Dreaming remains silent. Committed generations must mount before ordinary ingress
reopens, including recovery after a lost acknowledgement. Index recovery cannot
release a maintenance admission whose source writer is still held. Every admission
also has an opaque Core token: both original cleanup and recovery release that
captured token, so they cannot clear a subsequent owner's maintenance barrier.
Wake requires a fully initialized backend binding and the matching runtime
selector, not just a repository with the current generation id. Failed closes
remain owned by the backend until a later bind or shutdown succeeds. Pausing a
migration joins its worker and durably records the pause even when its caller
is cancelled. Candidate ownership is recorded before copying files, so failed
construction remains collectible under the existing retention policy.

Bunshin workflows hold durable generation references. Roles, retries and recovery
borrow that database read-only and reconstruct its declared embedding configuration;
there are no per-workflow or per-role database copies. Main-session writes within
that same generation remain visible. After swap, retired generations reject writes
and remain alive until logical references and actual reader leases drain. Bunshin
reconciles terminal and interrupted registrations independently of Dreaming. Global
forget tombstones remain authoritative. See [managed swap](../../../docs/pal_memory_swap.md).

Explicitly cancelled migration jobs remain durably paused until resumed. Recovery
retires superseded jobs before resolving their former backend. Memory's shared
worker join drains repeated cancellation for migration, CLI and Dreaming work;
candidate acquisition and final cleanup retain ownership until those workers exit.

Each generation pins one full embedding profile and one vector dimension. Reload
or runtime configuration drift cannot retarget its vectors. Mismatched or unknown
provenance exposes basic reads until a private-generation migration completes;
response and repository guards reject cross-model or cross-dimension results.
Dreaming publication and recovery rebind through the backend generation port;
the backend reloads embedding identity and catalog-owned indexing state together.
Vector inserts/updates maintain their native search entry in the write transaction.
Retirement removes native entries in the correction/merge transaction while
retaining original vectors for history. Index failures roll back those writes;
read-only readers do not depend on a later writer query to repair membership.

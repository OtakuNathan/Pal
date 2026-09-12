# Pal Dreaming

Dreaming is conservative duplicate consolidation. It does not periodically
rewrite, generalize, split, or polish memories. Facts must describe the same fact;
cases must describe the same event. A second LLM request reviews each proposed
transformation. Doubt means KEEP. Provider failure cannot bypass review.

The first implementation additionally requires every source in a case merge to
carry the same nonempty `payload.event_id` or `payload.source_event_id`. Similar
symptoms, identical repair text or nearby timestamps cannot establish this
identity. Legacy cases without an explicit identifier remain unchanged. Real-copy
inspection found that both processing and review models could otherwise approve
separate recurring incidents. This conservative eligibility rule sacrifices some
duplicate detection; an event identifier is still only a prerequisite, not proof
that a proposed merge preserves its information. Do not invent identifiers to
make old records eligible.

## Storage and daily writes

Long-term memory is owned by independent repositories under the runtime root:

```text
memory/
  archive.sqlite3
  generations/<generation_id>/memory.sqlite3
  runs/<run_id>/source.sqlite3
```

`pal.sqlite3` retains non-memory configuration. On startup, a restartable migration
copies legacy memory tables into the initial generation, checks copied values,
and rebuilds FTS. It preserves IDs, topics, usage metadata and reusable vector
blobs. Legacy tables remain a migration backup; ordinary runtime reads use the
published generation. Migration does not alter endpoint or channel configuration.

`pal setup --upgrade --runtime-root /path/to/runtime` explicitly performs the
same memory separation, followed by initial workflow pin registration. It holds
the existing runtime lease and refuses an active host before schema changes.
The initial startup compatibility path shares `MemoryStorage.migrate`; repeated
upgrades preserve the already published generation and do not copy legacy memory
over newer writes. Stop the service externally before this offline command.

`remember_memory` allows semantic duplicates. A scoped canonical-key conflict
returns the existing reference and requires an explicit update. Mechanical
deduplication compares all meaningful content and retrieval fields; case records
also need shared event identity. Stable invocation IDs make retries idempotent.

`update_memory` creates a successor ID and content revision. It stores the old
original and successor relationship in the same generation transaction. Retired
revisions do not participate in current recall. Their archive outbox is drained
incrementally; access counts do not create revisions.

FTS and pending embedding state commit with content. Vectors record the provider,
model, available model revision, text processing version, source hash and vector
dimension. A returned embedding is checked again before becoming current.
Changing embedding configuration rebuilds indices without rewriting text.

`memory_history` explicitly searches archived originals or resolves a historical
reference. Historical results are marked and are not installed into L2 HOT.
Original vector metadata and blobs remain with archived originals, alongside FTS.

Forgetting writes non-text tombstones before deleting originals, archive entries,
indices and dependent cached proposals. Old generation reads and L2 projection
honor tombstones. This is application deletion, not forensic disk erasure, and
does not retract information already present in a running conversation.
Pending archive revisions are readable by original/successor reference and by
history search before the next dreaming run. History responses omit vector blobs.
Forgetting also removes potentially quoted prose from run reports, retaining
aggregate diagnostics. Online copies inherit tombstones and observe subsequent
live deletions while their audit is running.

Candidate discovery caches bounded neighbor lists on the management side. It
refreshes changed records and their old/new neighborhoods; unchanged records do
not repeat vector/FTS discovery. Input, index or algorithm changes invalidate the
relevant cache. Previously checked primary pairs inform the next round's seed
order, allowing a former read-only neighbor to become a primary member in a
later round. This never expands the current round recursively or treats a
previous pair check as permission to merge; exact proposals still require their
full dependency-bound independent review.

## Scheduling and admission

Only the main Pal registers the independent `memory.dreaming` event source.
It shares cron calculation and Core wake deadlines with other event sources;
it is not a proactive prompt. Defaults:

```toml
[memory.dreaming]
enabled = false
timezone = "Asia/Shanghai"
cron = "0 3 1 * *"
endpoint_id = ""
review_endpoint_id = ""
neighbors = 12
max_group_members = 16
max_clusters_per_request = 3
input_tokens = 12000
output_tokens = 32768
request_timeout_seconds = 600
retention_days = 7
```

Empty endpoints resolve to the main endpoint at run start. Review uses an
independent request. The round locks endpoint/model facts, uses strict endpoint
selection, and fails if those facts change. Timeouts apply to individual requests,
not to the whole round. Schema repair has at most two additional attempts;
transport retries belong to the existing LLM runtime.

The cron starts preprocessing in a background thread. Missing multiple scheduled
occurrences coalesces to the latest occurrence. One nonterminal round is allowed.
Preprocessing uses bounded vector/topic/FTS neighbors within kind/scope/task,
then bounded seed groups with one primary owner per record. Cross-group references
are read-only. Cached decisions bind full inputs, references, candidate hash,
prompts, schemas and configuration. Whole originals are preserved in requests;
oversized material is split into bounded input groups or kept unchanged.

Core waits for main turns, queued input and admitted preparation/control work to
drain. Admission and the writer fence precede the sleep notice. Repository writes
also check the fence; generation writer locks cover independent connections.
Bunshin workers continue reading and do not block admission.

During sleep, user messages receive a fixed refusal and do not enter L1 or a
queue. Status/report remain available. New proactive execution is paused, and
its due state remains for the existing scheduler to coalesce on wake. Channel
delivery and recovery events continue. The sleep and wake notices use the same
persisted last accepted user route, including reply/thread targets. Missing route
or failed sleep delivery aborts before LLM processing. Wake delivery uses the
normal recovery queue after service admission has reopened.

There is no business cancel operation. Shutdown interrupts execution and records
recoverable failed work; it does not introduce a `cancelled` dreaming state.

## Publication and retention

No approved merge means `completed / no_changes`: same current generation, same
IDs and content, no replacement archive. The report says no safe duplicates were
confirmed, rather than claiming the whole database is duplicate-free.

With approved changes, build a private candidate from a consistent SQLite backup,
apply only reviewed replacements, prepare indices, validate reference coverage,
tombstones, integrity and retrieval, and open it privately through the repository
path. Persist the complete candidate before publication. One archive/catalog
transaction writes archive increments, successor relations, current generation
and the run's completed state. That transaction is the sole public commit point.

On failure before commit, current stays unchanged. After an uncertain commit,
the catalog is authoritative; recovery must not publish twice or pretend an
already exposed generation never existed. Startup recovers an abandoned writer
fence only after verifying that its owner lock has been released. A failed round
can reuse proposals only when complete dependency fingerprints still match.

The main Pal reconnects and clears only its L2 entries and heat. L1 survives.
Bunshin pins are durable per workflow: roles, retries, worker restarts and recovery
inherit the original generation. Only a truly new workflow chooses current.
Worker processes do not resolve current to replace a missing binding. Their
result metadata retains the generation; old projections do not replace main L2.

The current and immediately preceding published generations are retained.
Older generations need at least seven days since retirement and no live logical
pins or actual reader leases before collection. Terminal workflow pins are
released only when all logical sessions and assignments are terminal. A PID
disappearing is insufficient. Fix individual semantic mistakes through archived
originals, not by rolling back a whole publicly used database.

## Operation and activation

After the required sleep notice is delivered, Core broadcasts a retained
`runtime_state` snapshot (`sleeping: true`) to channel endpoints through
`on_runtime_state`. This UI signal is independent of the notification route and
does not enter chat history. Newly attached/replacement endpoints receive the
latest snapshot; providers replay it to reconnected clients. Maintenance exit
broadcasts `sleeping: false` only when service can reopen, including failed runs
that recover the original generation. A published generation that cannot mount
keeps the sleep state. Offline/online copy audits do not put the resident to sleep.

Resident commands:

```text
/dreaming status
/dreaming report [run_id]
/dreaming start --dry-run
/dreaming start
/dreaming resume [run_id]
```

The `memory_dreaming` capability exposes the same operations. Online dry runs use
isolated copies. An offline audit does not need a live notification route:

```sh
python -m pal.memory.dreaming --source-root ~/.pal --output-root /path/to/new/private/audit --preprocess-only
python -m pal.memory.dreaming --source-root ~/.pal --output-root /path/to/new/private/full-audit --endpoint configured-endpoint
```

The output directory must be new and outside the source runtime. Reports and
before/after databases stay local; they can contain private originals and must
not be committed to the source repository. `--preprocess-only` makes no LLM or
embedding requests. A full audit may publish inside its disposable copy; it never
changes online current or online archives.

This change touches resident Memory/Core code. Deployment requires an external
complete restart, preserving configuration and existing runtime data; plugin
attach alone cannot activate it. Keep automatic scheduling disabled until an
independent real-data audit and regressions have passed. Then enable the monthly
schedule; no monthly human approval is required. Linux validation does not imply
macOS connection, locking, or generation-collection validation.

## Validation record — 2026-09-11

Linux / Python 3.13.5 integration checkpoint: all four CI batches passed
(`core-a`: 601; `core-b`: 950 with 7 skips; `bunshin-a`: 465;
`bunshin-b`: 476). A Bunshin sandbox startup timeout under parallel test load
passed on isolated file and batch reruns. macOS and Python 3.12 were not run here.

Closing changes were checked with 107 memory/runtime/control regressions, 32
final dreaming/generation/admission tests, and two bootstrap memory round trips.
A later Core-A run passed 605 tests and found an event-ID field description
missing the required provenance wording; the corrected provenance test passed
separately. A redundant broader bootstrap rerun was interrupted; the earlier
complete batch and the final focused memory checks remain the verification basis.

The final independent real-data audit checked 540 active records (542 total,
including two retired records), grouped into 133 bounded clusters. Four fact
originals became two reviewed successors: 538 active records in the copy.
Legacy cases lacking explicit event identity stayed unchanged. All original
retrieval text survived these two merges, and four fixed lexical queries hit
the corresponding successors. These checks do not establish complete semantic
equivalence or prove the absence of remaining duplicates.

The audit used the configured `deepseek-v4.1-flash` endpoint for processing and
independent review: 10 requests, 115,522 input tokens and 46,900 output tokens.
Provider-reported zero cost is not a billing estimate. Earlier copy runs exposed
ambiguous case merges and an insufficient reasoning-output budget; those reports
are superseded by the final audit. Private originals and comparison reports stay
outside the repository.

For the inspected local installation, `pal.service` runs this checkout with
`--runtime-root /home/nathan/.pal`. Its configuration now enables the Shanghai
monthly schedule and locks both endpoints to the audited endpoint, with a 32,768
output-token ceiling. Other configuration was preserved. Source defaults remain
disabled. The running process has **not** been restarted and the online memory
database has **not** been migrated or consolidated by this audit.

External activation, after current Pal work finishes:

```sh
systemctl --user stop pal.service
pal setup --upgrade --runtime-root /home/nathan/.pal
systemctl --user start pal.service
systemctl --user is-active pal.service
```

Reconnect to Pal and send `/dreaming status`; on first activation expect `idle`,
`enabled: true`, and the migrated generation. Verify ordinary recall and a new
Bunshin workflow before treating the deployed behavior as validated. The next
scheduled preprocessing time, if activated before then, is October 1, 2026 at
03:00 Asia/Shanghai. Plugin reattachment is insufficient for this change.

## Review validation — 2026-09-12

Review repaired incremental candidate discovery so later rounds can examine
previously separated neighbors, while preserving bounded groups and read-only
references within each round. It also tightened deletion filtering in audit
copies and saved reports, pending-revision history, vector validation, cached
endpoint dependencies, and durable Bunshin generation/reference handling.
The conservative requirement for explicit shared case event identity remains
intentional; similar symptoms alone do not authorize a case merge.

If publication commits but the main provider cannot mount the published
generation, normal ingress stays closed. Status reports `service_ready: false`
and the activation error; recovery must mount the authoritative published
generation before reopening service.

`pal setup --upgrade` now performs the memory split under the runtime lease and
requires the Bunshin manager to be stopped. Repeated upgrade of a private copy
preserved the same generation and existing data. Normal bootstrap also completes
memory migration and existing workflow pin registration before optional plugins
can launch workers. No live migration or service restart was performed here.

The Linux / Python 3.13.5 review integration checkpoint passed all four batches:
`core-a` 616, `core-b` 952 with 7 skips, `bunshin-a` 465, and `bunshin-b` 476.
Subsequent fixes were checked with focused memory/dreaming tests, three upgrade
tests, three bootstrap tests, and the worker restart/reference test. These later
changes were not followed by another complete four-batch run. A private-copy
preprocessing run checked 540 active records, and the final offline upgrade
command succeeded again. No additional semantic LLM audit was run during review;
the earlier full-copy audit remains the semantic evidence. macOS and Python 3.12
remain unverified.

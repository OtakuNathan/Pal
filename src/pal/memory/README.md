# memory

Owns:
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
- compaction prompts, schemas, source selection, retry policy, or host
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
  replaces that history with a lossy continuity summary; RIGHT remains intact.
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
- Compact/reset archive only dialogue removed from L1 into credential-redacted
  daily JSONL under `data/conversation-history/`, by message date in Pal's timezone.
  Startup, shutdown and midnight do not archive active working dialogue.
  Searchable text excludes reasoning/native replay and does not replace recovery
- Core runs compaction and host policies render its checkpoint before storage;
  `MemoryService` performs the L1 replacement, dependent L2 cleanup, and
  execution-context projection inside one rollback boundary
- a storage or dependent-projection failure restores the original L1, L2,
  top-of-mind, and heat state

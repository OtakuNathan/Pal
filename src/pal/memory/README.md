# memory

Owns:
- `L1/L2/L3` memory semantics
- runtime-owned `L1` and `L2`, with one encrypted resident exit checkpoint
  consumed on the next Pal startup
- future durable `L3` repository boundary
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
- `MemoryCompactRequest` with a validated, rendered `summary_entry`
- `L3RepositoryPort`

Interaction rule:
- Pal and Bunshin instantiate the same `MemoryService` behavior and
  `MemoryPromptFragmentProvider`
- L1 is the complete working-set truth for the current logical process.
  Recall and L2 heat may be projected into prompts, but compaction never treats
  those projections as additional source records
- L1 accepts only closed tool protocol: every assistant tool-call batch must
  contain exactly its matching results in the same transcript. Orphan,
  incomplete, and late unmatched results are rejected before they enter L1
- L1 stores provider-neutral semantics only. A complete active-turn reasoning
  record may survive a closed-boundary checkpoint; incomplete stream/reasoning
  fragments and unmatched or duplicate tool protocol are removed mechanically
  during crash restore, which marks the damaged turn interrupted. Settled
  history is rendered as ordinary context, never replayed as an active
  provider tool envelope
- Memory exposes one runtime-state port for L1, L2, top-of-mind, and heat.
  Core orchestrates snapshot/restore/reset; Memory never reaches into Execution
- Resident Pal quiesces on SIGINT/SIGTERM and writes the fixed L1 runtime
  snapshot as one encrypted atomic checkpoint without calling an external
  model. Startup restores and consumes that checkpoint before channel
  endpoints start; normal context-budget policy may compact the restored L1
  later. There is no periodic checkpoint or background compaction loop
- Core runs compaction and host policies render its checkpoint before storage;
  `MemoryService` performs the L1 replacement, dependent L2 cleanup, and
  execution-context projection inside one rollback boundary
- a storage or dependent-projection failure restores the original L1, L2,
  top-of-mind, and heat state

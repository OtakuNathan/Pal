# Pal Migration Map

> Historical record, archived on 2026-10-05 from `docs/pal_migration_map.md`.
> Statements about implementation, deployment and test results describe the
> original work, not the current checkout or running host. See the
> [current documentation index](../../README.md).

> Goal: classify the current implementation for migration — clarify what gets rewritten, what gets wrapped-migrated, and what gets deleted.

## Classification Criteria

- `Rewrite`: current responsibilities conflict with the new skeleton; continuing to wrap would only deepen coupling
- `Wrapped migration`: core logic is still valuable, but boundaries need re-attachment
- `Keep`: already basically conforms to the new skeleton and can be migrated in directly
- `Delete`: semantics are deprecated or directly conflict with the new architecture

## Rewrite

## `src/pal/runtime/pal/app.py`

- Current responsibilities: overall assembly, main loop, capability invocation, memory orchestration, scheduler orchestration, worker orchestration, diagnostics orchestration
- Current problem: main orchestration and domain coordination are mixed in a single super entrypoint, with unclear boundaries
- Target home: `core` + `bootstrap` + several domain orchestrators
- Migration approach: split into `PalCore`, turn runner, and runtime wiring
- Preserve semantics: keep the main loop semantics, but not the current file shape

## `src/pal/runtime/pal/capabilities.py`

- Current responsibilities: single-point registration of memory, task, work order, proactive, introspection, and other capabilities
- Current problem: all domain capabilities are jumbled in one module, unable to reflect pluginization and subsystem separation of powers
- Target home: each subsystem's provider self-registers into `Execution`
- Migration approach: split into per-domain capability providers
- Preserve semantics: keep the capability family direction, but not the centralized registration pattern

## `src/pal/storage/sqlite.py`

- Current responsibilities: global database entry, migrations, memory, proactive, tasking, diagnostics, and channel route — all reads and writes
- Current problem: a single file handles persistence for all domains, making it the de facto system core
- Target home: `foundation/persistence` + per-domain repositories
- Migration approach: split into database infra and domain repositories
- Preserve semantics: keep SQLite as the storage backend, but not the global store shape

## `src/pal/runtime/pal/review_flow.py`

- Current responsibilities: work order review orchestration
- Current problem: belongs to the tasking domain, yet is placed in the pal runtime private directory
- Target home: `tasking`
- Migration approach: migrate directly per the old semantics, re-attaching to the tasking plugin family
- Preserve semantics: keep the review semantics, do not redefine the review contract

## `src/pal/runtime/pal/worker_manager.py`

- Current responsibilities: worker spawn, worker listener, closeout, failure handling
- Current problem: belongs to the tasking/execution world and should not sit under the pal runtime private entrypoint
- Target home: `tasking`
- Migration approach: migrate directly per the old semantics, splitting into a worker orchestration service
- Preserve semantics: keep worker continuity, checkpoint, and approval handoff semantics

## Wrapped Migration

## `src/pal/memory/*`

- Current responsibilities: memory models, manager, compact, render, repository
- Current problem: has some abstraction already, but still mixes in the old bucket model and conversation-scoped durable host assumptions
- Target home: `memory`
- Migration approach: keep the reusable leaf logic, rewrite the `L1/L2/L3` contract and `MemoryService`; split the `L3` truth model from the unified `pal_memories` into `memory_facts` / `memory_cases`, and consolidate the topic index into `memory_topics`
- Preserve semantics: keep the main concepts of compact/recall/commit/correct/retire, but not the old page_fault and durable L2 semantics; do not keep the three-track coexistence semantics of `tags + memory_tags + topic_tags_blob`

## `src/pal/execution/*`

- Current responsibilities: tool registry, tool executor, policy, skills, runtime factory
- Current problem: the underlying execution surface is fairly clean, but capability/plugin/introspection are not yet first-class
- Target home: `execution`
- Migration approach: keep the underlying tool execution facilities, and add capability routing, plugin runtime, and introspection index on top
- Preserve semantics: keep `Tool` as the sole execution primitive

## `src/pal/channel/*`

- Current responsibilities: channel adapter, normalize, route, mailbox, slash commands
- Current problem: the main direction is correct, but there are still old bindings to route durability and conversation identity
- Target home: `channel`
- Migration approach: keep the I/O and normalize core, changing it to depend on `EndpointConfig` and `ResponseHandle`
- Preserve semantics: keep the main paths of receive / send / segmented send / reply route

## Legacy `proactive` schema / store paths

- Current responsibilities: `proactive_definitions`, `proactive_runs`, schedule computation, proactive run bookkeeping, output channel / reply target persistence
- Current problems: the old naming has been retired, and the durable hot-state table is no longer needed
- Target home: `proactive`
- Migration approach: keep only the `proactive_definitions/proactive_runs` active schema; hot/top-of-mind state stays in runtime memory
- Semantics preserved: keep schedule, due trigger, run lifecycle, and output routing; do not keep `service` as the subsystem name

## Wrapper migration

## `src/pal/llm/*`

- Current responsibilities: provider-neutral IR, wire-shape codec, explicit endpoint selection, and same-endpoint retry
- Current problems: the legacy provider adapters and canonical DTOs have been replaced by the immutable IR and a unified JSON-frame decoder
- Target home: `llm`
- Migration approach: use the OpenAI/Anthropic SDKs to carry the three fixed wire shapes; the local `llm_endpoints` is the source of truth for model routing and capabilities; both streaming and single-shot are normalized to JSON frames first
- Semantics preserved: keep fallback and the local tool execution boundary; do not keep provider behavior branching, legacy DTOs, or fake streaming

## Keep

## `src/pal/ipc/*`

- Current responsibilities: peer transport, mailbox, codec
- Current problems: this is infrastructure and should not be repeatedly rewritten by higher-level architecture
- Target home: `foundation/io`
- Migration approach: move in directly
- Semantics preserved: keep

## `src/pal/io/*`

- Current responsibilities: process runner, pipe helpers
- Current problems: strongly infrastructure-like
- Target home: `foundation/io`
- Migration approach: move in directly
- Semantics preserved: keep

## `src/pal/security/*`

- Current responsibilities: credentials, keychain
- Current problems: no direct conflict with the new skeleton
- Target home: infrastructure or a standalone security family
- Migration approach: move in directly
- Semantics preserved: keep

## `src/pal/diagnostics/*`

- Current responsibilities: failure reporting and user feedback rendering
- Current problems: it is still a runtime helper, not a formal failure constitution
- Target home: diagnostics / reporting domain
- Migration approach: keep the current data-object direction and upgrade it into a formal failure reporting contract
- Semantics preserved: keep and extend

## Delete

## `src/pal/retrieval/page_fault.py`

- Current responsibilities: the legacy page fault candidate selection
- Current problems: it was built on the old philosophy of "the model silently deciding what to recall"
- Target home: none
- Migration approach: delete
- Semantics preserved: not preserved

## Overall assessment of the current implementation

The three coupling points most urgently in need of decomposition are:

- `app.py`
- `capabilities.py`
- `sqlite.py`

The leaf capabilities most worth keeping and re-homing are:

- `channel` I/O handling
- `execution` low-level tool execution facilities
- `llm` canonical/provider layer
- `ipc` / `io` / `security`

The assessment of the tasking line is emphasized separately as follows:

- the task subsystem's semantics were already fairly clear in the old version
- the new architecture does not reinvent the business meanings of task / worker / work_order / approval / checkpoint / ledger
- The main action of this part is an ownership refactor, not a domain semantic rewrite

Legacy document semantics that have proven stable and should be considered as migrated directly into the new architecture also include:

- `supervisor -> pal -> worker` process model
- The IPC layering between the `supervisor-pal` control plane and the `pal-worker` execution plane
- `Pal`-owned user-facing channel
- provider-neutral LLM IR + three wire-shape codecs + exact-model request hooks
  + provider-specific response-iterator normalization
- `skill = manual`, `tool = the sole execution primitive`
- `L1` as a near-lossless compressed transcript, not a summary bucket

## Invariants

- No global super store is retained.
- No single-point capability registrar is retained.
- No `page_fault` is retained.
- `Tool` remains the sole execution primitive.
- `L3` remains the memory owned durable layer.

## Non-Goals

- This document does not define the specific order of code changes
- This document does not define how each test is migrated
- This document does not promise compatibility with the old schema or old CLI

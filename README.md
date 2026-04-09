# PalV2

`PalV2` is an isolated rebuild lane for the new architecture.

The goal is to let the new system evolve independently from the legacy `pal`
runtime, with its own package layout, dependencies, tests, and persistence
layer.

Current direction:

- SQLite via `peewee`
- per-domain repositories
- raw SQL reserved for features `peewee` cannot model cleanly, such as FTS,
  projection views, and future vector indexes
- schema migration handled by external scripts, not by the PalV2 runtime

For local development and architecture bring-up, `PalV2` also exposes a
stub provisioning + runtime composition path:

- `supervisor` owns the runtime-root to database-file association
- `supervisor.provision_stub_runtime(...)` creates the database and seeds
  default identity / channel / llm records
- `bootstrap.compose_runtime(...)` composes the in-process runtime from that
  provisioned database
- the composed runtime returns a ready-to-drive `PalCore` plus the key stub
  services

## Core Rule

`PalV2` uses a split responsibility model:

- `PalCore` owns governance
- `Execution` owns invocation

That means:

- `Pal` reaches capabilities through `PalCore -> Execution`
- `Execution` may physically store the capability registry and invoke registered
  callables
- `PalCore` decides what gets registered, published, detached, reattached, or
  withdrawn
- modules must not register themselves directly into each other or call each
  other's internal runtime/service/repository surfaces
- `supervisor` is outside the Pal runtime and is not part of `PalCore`
  governance or the in-process `MainLoop`

In short:

- `Execution` knows how to call
- `PalCore` knows what is allowed to be called

Capability names should use the canonical namespace-first form:

- `introspection.<scope>.<module>.<action>`
- `operation.<module>.<family>.<action>`

Examples:

- `introspection.module.channel.list`
- `introspection.endpoint.channel.inspect`
- `operation.channel.management.attach`
- `operation.execution.exec.run`

This naming rule is constitutional:

- namespace is always explicit
- module is always explicit
- family is explicit on the `operation` line
- action names may repeat across modules, but canonical paths remain stable and non-conflicting

L3 memory providers are treated as an execution-owned plugin family:

- `Execution` always carries a default stub provider: `null_l3`
- `memory.introspection.configure` can switch the active provider to a real
  backend
- `memory.introspection.configure` can also fall back to `null_l3`

Prompt assembly uses an explicit fragment provider registry:

- modules register `PromptFragmentProvider` with `PalCore`
- `PalCore` collects only registered fragment providers
- modules do not implicitly participate in prompt assembly
- `PalCore` is the only prompt assembler

Turn execution uses a mailbox-driven computation model:

- `channel` owns ingress normalization and reply outbox handoff
- `MainLoop` polls mailbox-backed event sources
- `PalCore` owns turn suspend/resume and orchestration
- `Execution` interprets turn effects such as `llm.request`, `tool.call`, and
  `mailbox.reply`
- a turn is considered reply-complete once the final response is accepted by
  the channel outbox
- delivery success/failure is tracked later as channel-side diagnostics

Tool-loop stagnation is guarded by a dedicated process:

- the guard evaluates canonical tool signature hashes plus normalized result
  fingerprints
- it can force `PalCore` into `finalization_only` mode
- in `finalization_only`, the next `llm.request` is physically disarmed by
  sending no tools and forcing a text-only final reply attempt

Capability discovery and execution now use a unified Capability Forest:

- physical structure is one forest, not two separate tree implementations
- logical namespaces are:
  - `introspection`
  - `operation`
- decorators define static blueprints
- `register_with_core(...)` hydrates those blueprints against runtime
  instances
- `Execution` compiles hydrated subtrees into:
  - a fuzzy search index for discovery
  - an O(1) dispatch table for execution
- alias/search and execute are strictly separated
- instance-level actions auto-inject `target_id` into the LLM-facing schema
- module-level actions use `SINGLETON_TARGET="__singleton__"` internally

Capability Forest deep-dive:

- [Capability Forest Structure](./docs/capability_forest_structure.md)
- [Turn Runtime Structure](./docs/turn_runtime_structure.md)

Capability Forest constitutional rule:

- parent nodes govern only their direct children
- no node may skip levels and directly manage deeper descendants
- generic governance stays at the parent level
- concrete configuration must sink into the corresponding child subtree
- control-plane slash commands are runtime-private governance ingress
- slash commands may affect policy outcomes, but their raw command text must
  never be exposed to LLM-visible capability surfaces
- slash commands must not be persisted into L1 as conversational memory

Channel-specific application of that rule:

- `channel` root manages endpoint membership and availability only
- endpoint nodes own their own introspection/configuration surface
- endpoint nodes do not self-govern `attach/detach`
- channel endpoints are visible to `Pal/LLM` only through management and
  introspection; direct transport operations stay runtime-private
- secrets/tokens are never returned through endpoint introspection

Identity-specific application of that rule:

- `identity` is always-on foundation state
- `Pal/LLM` may query identity, but may not modify it through capability calls
- `identity` does not expose lifecycle capabilities
- any runtime tuning derived from model output must live in runtime-private
  orchestration, not inside `identity`

## Feature Extension Rule

When adding a new feature surface to `PalV2`, prefer concrete capability names
over vague `observe/configure` placeholders.

The rule of thumb is:

- introspection nodes should expose specific read-oriented actions such as
  `list`, `inspect`, `health`, `backlog`, or `auth_state`
- management nodes should expose specific governance actions such as `enable`,
  `disable`, `attach`, or `detach`
- new business behavior belongs on plugin/provider operation nodes, not on
  module roots

All callable surfaces still use the shared minimal envelope style:

- call object: `name`, `args`, optional `meta`
- result object: `status`, `text`, optional `structured`

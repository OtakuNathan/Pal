# Capability Forest Structure

This document describes Pal's unified capability forest: why it exists, how
blueprints hydrate into runtime capabilities, and how discovery remains separate
from execution.

## Summary

The capability forest is Pal's runtime source of truth for capability structure.
It organizes, compiles, and routes capabilities, but it does not make governance
decisions.

Responsibilities:

- `PalCore` decides what is mounted, withdrawn, detached, or reattached.
- `Execution` owns the forest and compiles it into search and dispatch indexes.
- Providers define capability blueprints and runtime hydration logic.

## Control Commands Are Not LLM Capabilities

The forest contains two LLM-visible capability namespaces:

- `introspection`
- `operation`

Runtime-private registered slash commands are not part of either namespace.

Rules:

- Registered slash commands are deterministic control-plane ingress.
- Matched raw command text does not enter the LLM prompt.
- Matched raw command text is not written to L1 as conversation memory.
- Slash-like text that does not match any registered command or alias is ordinary user text and follows the normal conversational path.
- The LLM may observe resulting governance state, such as "tool use disabled",
  but not the raw `/pause-tools` or `/detach ...` command text.

## Why Not Hand-Written Flat Descriptors

A flat list of `CapabilityDescriptor` records is too weak for Pal's runtime
shape.

Problems with a flat-only model:

- module authors hand-build descriptors inconsistently
- instance-level targets are hard to represent
- dynamic attach/detach semantics become scattered
- stable execution keys and human-readable names drift apart

Current model:

- static source of truth: `CapabilityNodeBlueprint`
- runtime source of truth: hydrated capability subtree
- LLM/search-facing projection: compiled capability descriptors

## Physical Structure

There is one `CapabilityForestRegistry`, not separate implementations for
introspection and operation.

The namespace roots are logical:

- `introspection`
- `operation`

One physical forest allows shared indexing, mounting, hydration, and dispatch
while preserving namespace separation in compiled names.

## Core Objects

### `CapabilityNodeBlueprint`

Source: [capability_forest.py](../src/pal/shared/capability_forest.py)

A static template describing:

- namespace
- node kind, such as module, endpoint, provider, or singleton target
- source module/provider
- target identity extraction
- child node shape

Blueprints are not runtime state.

### `CapabilityActionBlueprint`

A static action template describing:

- action name
- family
- aliases
- argument schema
- result schema
- handler binding metadata
- search/display metadata

Action blueprints compile into LLM/search-visible capability records and runtime
dispatch bindings.

### `HydratedCapabilityNode`

A runtime node produced from a blueprint and a concrete provider/module
instance.

It carries:

- `target_id`
- `target_label`
- live mounted status
- child runtime nodes
- action bindings for this concrete target

### `MountedSubtreeHandle`

A mount handle returned when a subtree is published into the registry.

It is used to:

- withdraw a mounted subtree
- rehydrate after endpoint/provider changes
- keep core governance separate from provider implementation

### `CompiledCapabilityIndex`

The searchable, LLM-facing projection.

It answers:

- what capabilities exist
- what each capability is called
- what arguments are required
- what namespace/family/module/tags can filter the capability

It is optimized for discovery and prompt/tool schema generation, not execution.

### `BoundActionIndex`

The dispatch projection.

It maps canonical path/target pairs to concrete runtime handlers. The compiled
registry resolves public aliases before dispatch. This index serves exact
invocation, not broad search.

## Naming

Pal separates stable execution keys from human-readable names.

### Canonical Path

The canonical path is the stable execution key, such as:

- `op_tool_search`
- `op_tool_read`
- `op_browser_read`

It should be:

- stable
- unique
- namespace-prefixed
- suitable for exact dispatch

### Display Name

The display name is a human-facing label. Model invocation uses the public alias,
which must be unique within its registry generation. Canonical paths are internal
identities, not model-callable names.

### `omit_family_in_canonical`

Some high-frequency capabilities intentionally use compact canonical names.

Example:

- capability family/action may be `discovery/search`
- canonical path is explicitly `op_tool_search`; the public alias is `search_tools`

This is controlled by metadata, so compact names are explicit and testable.

## Instance-Level Targets

Instance-level capabilities receive a model-facing selector through hydration.

Examples:

- channel endpoint management
- proactive task inspection and run history

The compiler injects the declared `target_argument` (normally `name`) into
instance-level action schemas. Internally, dispatch still uses `target_id`.
Module-level aggregate tools such as `inspect_llm` do not acquire an instance
selector merely because their output describes several objects.

## Module-Level Targets

Module-level actions use a singleton target internally, even when no user-facing
`target_id` is required.

This keeps dispatch uniform:

- every action has a target
- module actions target the module singleton
- instance actions target a concrete endpoint/provider/plugin instance

## Hydration Flow

1. Module import defines static blueprints and action blueprints.
2. `register_with_core(instance)` registers provider/module objects with the
   Pal context.
3. PalCore publishes selected module/provider subtrees.
4. Execution hydrates blueprints against live runtime instances.
5. Execution compiles:
   - searchable capability specs
   - LLM tool schemas
   - dispatch bindings

## Search And Execute Are Separate

### Search

Search answers discovery questions:

- What capability names match this query?
- Which namespace should I use?
- Which module/family/tags narrow the result?
- What required parameters does a capability need?

`search_tools` returns callable hits with guidance and input contracts:

- `alias`, `domain`, `purpose`, `use_when`, `do_not_use_when`
- `invocation_mode`, `input_contract`, `execution`
- matched and unmatched terms when applicable

It accepts:

- `query`
- `domain`: strict operation-owner bucket
- `namespace`: `inspect`/`introspection` or `action`/`operation`
- `family`
- `module_name`
- `tags`
- `top_k` / `limit`
- `facets`
- `directory_offset`

`search_tools(domain=...)` without query browses a compact domain directory;
`search_tools(directory_offset=0)` without query/domain lists available domains.
Directory entries are navigation, not query matches or full contracts.

`facets` defaults to false. When true, the result includes namespace, module, and
family counts over the deduplicated candidate set. Facets are a narrowing aid,
not the default answer shape.

### Execute

Execute answers exact dispatch questions:

- Is this capability registered now?
- Is the target mounted and live?
- Are the arguments valid?
- Which handler receives the call?

`call_tool(name=alias, args=...)` invokes an indirect capability by its exact
public alias. Tools whose complete native schemas are already exposed are
invoked directly. Use `read_tool` only when a known tool's contract information
is missing. Neither entrypoint accepts canonical paths as model-callable names.

## Module Nodes And Plugin Nodes

Module nodes represent first-party Pal subsystems such as:

- `llm`
- `memory`
- `channel`
- `execution`
- `bunshin`

Plugin/provider nodes represent concrete providers under those subsystems, such
as:

- a Telegram channel endpoint
- a sqlite-vec L3 provider
- an MCP server projection

The module owns common governance and discovery. The provider owns concrete
implementation mechanics.

## Reading Order

Recommended code reading order:

1. [capability_forest.py](../src/pal/shared/capability_forest.py)
2. [capability_compiler.py](../src/pal/execution/capability_compiler.py)
3. [execution/runtime.py](../src/pal/execution/runtime.py)
4. [core/main_context.py](../src/pal/core/main_context.py)
5. [core/runtime.py](../src/pal/core/runtime.py)
6. Provider examples:
   - [channel/capabilities.py](../src/pal/channel/capabilities.py)
   - [llm/capabilities.py](../src/pal/llm/capabilities.py)
   - [plugins/l3/stubs.py](../src/pal/plugins/l3/stubs.py)

## Provider Contribution Rule

First-party modules own their capability trees. Runtime plugins may publish
capabilities through their provider boundary, but they do not mutate another
module's forest directly.

LLM wire rendering belongs to the three codecs in `src/pal/llm/shapes/`, not in
the generic capability forest. Exact-model tuning belongs to runtime model
hooks. Provider identity may select its explicit response-normalization hook;
it does not choose wire semantics, codec, authentication or transport.
Exact-endpoint response hooks compose after provider defaults and before the
common semantic decoder. See the [LLM hook contract](pal_llm_contract.md#provider-and-endpoint-response-hooks).

## Invariants

- One physical forest, multiple namespace roots.
- Search and exact dispatch are compiled separately.
- Canonical paths are stable execution keys.
- Display names are human/model-facing descriptions.
- Instance actions receive `target_id` automatically.
- Control-plane slash commands are not LLM-visible capabilities.
- Provider-specific protocol quirks do not belong in the forest.

# Pal Execution Contract

This document describes the current Execution boundary. Concrete descriptors, handlers and result types
are defined by [execution/contracts.py](../src/pal/execution/contracts.py), the
[tool facade](../src/pal/execution/tool_facade.py) and the
[shared Tool Protocol](../src/pal/shared/tool_protocol.py).
For the tool inventory and discovery rules, see [Tool Surface](pal_tool_surface.md); no fixed count is maintained here.

## Ownership

Execution owns the Capability Forest, immutable registry generations, discovery and dispatch
indexes, argument/result validation, effect/retry semantics, file permissions, and result snapshots.

- Core decides mounting, admission, and turn effect scheduling.
- Execution executes approved capability invocations.
- PluginHost owns plugin discovery, code generations, resources, and cleanup.
- Each domain provider owns its business source of truth, SDK, and private state.
- Skill provides manuals; it grants no tool permissions and owns no side-effect execution.

`Execution` holds no conversation history, long-term memory, channel transport, or workflow ledger.
All tool calls produced by the model must pass through the local execution boundary; provider-native tool calling
does not constitute a bypass. A module's own connections, storage, and lifecycle I/O remain managed by its owner.

## Capability Descriptor

Pal-native capabilities are published via a `CapabilityDescriptor` and bound actions:

- `name`, `family`, and `source` mark identity and ownership; family may only come from the owner's explicit declaration,
  is empty when undeclared, and must not be guessed from the namespace/module.
- `aliases` must contain exactly one non-empty public alias, unique within the registry generation.
- `canonical_path` and `target_id` are internal routing metadata, not model call names.
- `guidance: ToolGuidance` provides purpose, use-when, restrictions, search terms, and next-tool relations;
  model descriptions and search documentation are compiled from it, with no parallel prose contract maintained separately.
- `InputModel` / `OutputModel`, `execution`, and `examples` declare types and execution semantics.
  A non-empty internal input model requires at least one example that passes strict validation.
- `module_id`, `detachable`, and lifecycle/target metadata serve publication and unloading boundaries.

Internal tools use Pydantic v2 strict models; ordinary object models forbid extra fields.
MCP capabilities are an explicit exception: the server-side JSON Schema is preserved, not disguised as an internal Pydantic
model; calls and results still pass through Pal's admission, validation, and effect boundaries.

## Registry Generation

A publication compiles the forest, bound actions, aliases, search records, and provider specs
into a single `ToolRegistryGeneration`, then atomically replaces the current pointer.
Model schemas, search results, and actual dispatch must come from the same generation.

The capability tree and instance targets are generated via declared blueprint/hydration.
Providers must not directly modify another module's registry. Internal manager routing uses
`canonical_path + target_id`; the model uses the exact public alias.
See [Capability Forest](capability_forest_structure.md) for details.

## Discovery-First Rule

The model's tool window is determined by the descriptor's execution semantics:

- `DIRECT` exposes the full native schema.
- An explicit `metadata.native_function=True` may place an `INDIRECT` schema into the native window,
  retaining the original routing, effect/retry, and permission checks.
- All other `INDIRECT` capabilities are first discovered via `search_tools`, then invoked via `call_tool(name=alias, args=...)`.
  Use `read_tool` only when required contract information is missing.

A canonical path cannot serve as a model call alias. There is no generic tool-surface config or refresh
command; plugin publication updates the registry, while resident implementation changes require a host restart.
Missing optional/role-scoped next-tool hints must not recommend unavailable capabilities to the model.

Skill discovery is a separate `search_skills` / `read_skill` manual path; it cannot substitute for
capability availability or call authorization. The behavior advisor has been removed.

## Invocation And Handler Contract

`CapabilityCall` is the internal input envelope containing `name`, `args`, and `meta`.
It does not require the model to convert all arguments to strings; objects, lists, booleans, enums, and null all follow the tool
schema. When the model sends an object, it must not re-encode it as a JSON string.

Pal-owned handlers bind strict inputs and outputs through a declared facade/action.
Long-lived dependencies are injected from the owning service/port, not obtained by guessing signatures or reading another module's private fields.
The Runtime validates parameters, execution permissions, effects, and retry policy before invoking the bound handler.

When invoking an async tool or running a synchronous handler, the lifecycle fence holds until the actual handler exits.
Cancelling an awaiting call must not unload code while its handler is running; queued calls cancelled before dispatch never start.
Exceptions are returned via the unified failure contract and preserve the original cause; wrapper failures must not erase business results.

## CapabilityResult

The internal `CapabilityResult` has at least `status` and a non-empty `llm_text`, and may additionally carry `text`,
`structured`, effect receipt, affordances, recovery hint, context delivery,
context messages and snapshot refs. It is not an arbitrary dict that merely contains `status/text/structured`.

The Runtime only recognizes explicitly declared `CapabilityResult`, `ToolHandlerResult`, and invocation
result envelopes, or validates raw output against the original tool's output schema. Objects whose fields merely "look like" an envelope
are not specially accepted.

The cross-domain Tool Protocol IR comes uniformly from `pal.shared.tool_protocol`: tool definitions,
calls, transcript results, execution outcomes, and recovery prompts must not be redefined in individual domains.
A received call must already have an explicit call id; only Pal-originated calls may assign a new id.

## Failure, Effect And Retry

Model invocation results distinguish `complete`, `rejected`, and `failed`, and explicitly express the effect outcome
and retry direction. The original exception chain, owner diagnostics, receipt, and recovery
information must pass through the adapter and must not be overwritten by later output faults.

A completed business action may additionally carry a `delivery_error`, indicating a secondary failure of the result output or guidance.
It does not change the state or effect of the original action, nor authorize redoing completed side effects.
Short reasons and recovery directions for errors do not consume the normal output budget; longer diagnostics are preserved via snapshot.
All model-visible diagnostics undergo credential redaction first; private host evidence remains managed by the owner.

See [Result Guidance](pal_result_guidance_contract.md) and
[Failure Reporting](pal_failure_reporting_contract.md).

## Output Budgets And Snapshots

The full result is validated first, then the output budget is applied. Large outputs are written as immutable UTF-8 snapshots, delivering a bounded
head/tail preview and the local path; the model uses `rg` or `read_file` to view the full content.
Pagination rules for business queries are not replaced by the output budget.

If snapshot saving fails, the full inline result must be delivered together with an explanation of the storage error and budget
exception; leaving only a preview without a complete copy is not allowed.

Tool-result delivery metadata is stored in the L1 `ToolResultIR`. Snapshot ownership comes from
explicit L1 references, pending deliveries, and in-flight request pins; an elapsed-turn TTL is not used.
Core treats L1 delivery and Execution file authority as a joint rollback boundary: if either side
rejects a late/malformed delivery, the other side also reverts, and uncommitted snapshots are retired.

See [Tool Output Snapshots](tool_output_snapshots.md).

## Local Files And Shell

Regular UTF-8 file reads, precise edits, and writes use `read_file`, `edit_file`, `write_file`.
Commands, tests, builds, scripts, and repository text search use `run_shell`; prefer `rg` for search,
and `rg --files` for file enumeration.

File tools preserve read-before-edit and version permissions; a delivered read result holds the corresponding grant
until its L1 owner is retired by compact/reset. CRLF/CR line endings are not rewritten by display line numbers.
Remote file routing and shell transport each go through explicit contracts; the client must not use shell
to reimplement its own file read/write semantics.

[pal-shell-native](https://github.com/OtakuNathan/pal-shell-native) is an optional standalone
shell implementation that owns sessions, PTY, and remote routing. It receives declared framework state via
the [Execution Extension](execution_extensions.md);
private implementation state is not copied across modules from the host.

## Plugin Refresh Rule

No filesystem watcher auto hot-reload is performed. Plugin manifest and implementation changes go through explicit PluginHost operations:

| Operation | Semantics |
| --- | --- |
| `rescan_plugins` | Discovers metadata and physical removals; does not replace code currently being loaded |
| `manage_plugin(name, operation="attach")` | Loads an enabled detached plugin; keeps the existing instance if already attached |
| `manage_plugin(name, operation="reload")` | Replaces the implementation generation, running cleanup, unload, load, and attach |
| `manage_plugin(name, operation="enable"/"disable")` | Manages enablement state and the corresponding lifecycle |
| `manage_channel_endpoint(name, operation="restart")` | Rebuilds a single endpoint transport without reloading provider code |

The old `/plugins refresh` is not part of the current contract; do not derive unregistered slash commands from it.
New plugins implement `build_plugin(context)` and `start(scope)`; side-effect resources are only acquired during the start
phase, and become externally visible only after publication succeeds. A failed reload does not guarantee rollback to the old version;
check load/cleanup errors and representative behavior; copied files must not be equated with loaded code.
See the [Plugin README](../src/pal/plugins/README.md),
[Package Installation](pal_package_installation.md).

## Providers And Delegation

Retry rules belong to their respective domains. The LLM retries only the selected endpoint; web search has its own fallback
chain; the stateful browser does not masquerade as a generic fallback family. Embedding-space changes require an explicit
migration; models must not be swapped arbitrarily per single call.

Long-running or specialized work enters manager-owned durable execution through Bunshin Task/workflow.
[Planned orchestration](bunshin_contract_orchestration.md) and
[direct mode](bunshin_direct_mode.md) share candidate, verification, and publication
boundaries. The old work-order / `tasking.*` is not the current model interface.
Execution does not own the ledger, plans, role leases, or human review decisions on behalf of Bunshin.

## Introspection

Self-observation goes through published object-level tools, such as `observe_core`, `inspect_plugin`,
`inspect_channel_endpoint`, `inspect_memory_state`, `inspect_llm`.
Backend maintenance and configuration are done by the owning CLI or lifecycle tools; inspection does not constitute mutation authorization.

There is no unified runtime API requiring all modules to implement same-named `observe/modify/debug/extend` methods,
nor may one assume, based on early designs, an invocable alias such as `introspection.self_diagnose`.
The current inspection surface is defined by live discovery; see the [Introspection Contract](pal_introspection_contract.md).

## Invariants

- A single registry generation simultaneously constrains discovery, model schema, and actual dispatch.
- The public alias is the model-facing call name; the canonical path/target id is the internal identity.
- Parameters and results are validated against the declared schema, not by string guessing or envelope duck typing.
- Effect, retry, raw failure, and output faults are expressed separately, without implying replay of completed actions.
- L1 and Execution share a commit/rollback boundary for result delivery, snapshots, and file permissions.
- PluginHost owns the code generation; Core coordinates; Execution invokes; business state is left to the domain owner.
- Native LLM/MCP protocols and external SDKs do not bypass local governance.

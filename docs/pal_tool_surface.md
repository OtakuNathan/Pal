# Pal Tool Surface

`ToolSurface` projects registered capabilities into the LLM tool window and selects the reduced tool surface for failure-recovery turns.

Pal may register many capabilities, but only the direct set enters the LLM tool window. Everything else remains in the execution inventory and is discoverable through capability search.

## Source Of Truth

The resident surface is fully determined by capability descriptors at registry compile time:

- Every `CapabilityDescriptor` declares `invocation_mode`: `DIRECT` or `INDIRECT`.
- `DIRECT` descriptors are compiled into the registry generation's `provider_specs` and exposed to the LLM as function-calling tools.
- `INDIRECT` descriptors stay out of the tool window. They remain discoverable through `tool_search` and invocable through `read_tool` / `tool_call`.

There is no tool-surface config file and no runtime refresh command. Changing direct exposure is a descriptor change in the owning module (applied on the next Pal start), not a TOML edit.

## Failure Surface

`select_failure_descriptors` builds a small, subsystem-scoped tool set for failure-recovery turns (for example memory provider introspection when the failing subsystem is `memory`). This selection lives in code, is keyed by canonical paths, and is independent of the normal-turn surface.

## Discovery Schema

`tool_search` is the resident discovery entry point. Its primary argument is
`query`, not `name`.

Important arguments:

- `query`: natural-language search text or a partial capability name
- `namespace`: `intro`/`introspection` for inspection capabilities, or
  `op`/`operation` for mutating/external-service actions
- `family`: optional family filter
- `module_id`: optional module filter
- `tags`: optional tag filters
- `top_k` / `limit`: compact hit count
- `facets`: defaults to false; when true, include namespace/module/family counts
  for broad-search narrowing

The default result should stay compact and return top hits only. Facets are
available when the model needs narrowing statistics, but they should not be
returned by default.

## Artifact Tool Boundary

Artifact tools accept `artifact_id`, not arbitrary local paths.

`artifact_grep` searches existing text-like representations only. It does not inspect image pixels, perform OCR, or create audio transcripts. If an artifact needs OCR, ASR, PDF parsing, or image processing, Pal must discover a suitable capability for that representation or path.

## MCP Tool Boundary

MCP tools are not resident by default. The MCP manager plugin compiles discovered server tools into Pal-native capabilities and publishes them into the capability inventory.

MCP prompt templates become declared skills plus render capabilities. They do not enter the resident prompt automatically.

## Invariants

- The resident tool set is exactly the set of `DIRECT` descriptors; nothing else enters the LLM tool window.
- All `INDIRECT` capabilities remain discoverable through execution discovery.
- Tool exposure is not capability availability. Availability is runtime state and must be inspected when it matters.
- External protocol surfaces such as MCP must not bypass Pal execution, approval, or capability policy.

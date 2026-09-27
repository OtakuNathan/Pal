# Pal Tool Surface

`ToolSurface` projects registered capabilities into the LLM tool window and selects the reduced tool surface for failure-recovery turns.

Pal may register many capabilities, but only the direct set enters the LLM tool window. Everything else remains in the execution inventory and is discoverable through capability search.

## Source Of Truth

The resident surface is fully determined by capability descriptors at registry compile time:

- Every `CapabilityDescriptor` declares `invocation_mode`: `DIRECT` or `INDIRECT`.
- `DIRECT` descriptors are compiled into the registry generation's `provider_specs` and exposed to the LLM as function-calling tools.
- `INDIRECT` descriptors stay out of the tool window. They remain discoverable through `search_tools` and invocable through `call_tool`. Use `read_tool` only when the search hit or context lacks contract details needed for a correct call.

There is no tool-surface config file and no runtime refresh command. Changing direct exposure is a descriptor change in the owning module (applied on the next Pal start), not a TOML edit.

## Failure Surface

`select_failure_descriptors` builds a small, subsystem-scoped tool set for failure-recovery turns (for example memory provider introspection when the failing subsystem is `memory`). This selection lives in code, is keyed by canonical paths, and is independent of the normal-turn surface.

## Discovery Schema

`search_tools` is the resident discovery entry point. Its primary argument is
`query`, not `name`.

Search with English alias words in `[domain] [action] [object]` form, such as
`remember memory`, `lsp incoming calls`, or `browser screenshot`. Exact aliases
and alias prefixes also work. Search
matches words, including words separated by underscores in aliases; `install`
does not match `uninstall`. Broad or unmatched queries can be refined using
the result's hit and filter guidance.

Important arguments:

- `query`: natural-language search text or a partial capability name
- `namespace`: `intro`/`introspection` for inspection capabilities, or
  `op`/`operation` for mutating/external-service actions
- `family`: optional family filter
- `module_name`: optional module filter
- `tags`: optional tag filters
- `top_k` / `limit`: hit count, default up to 3 strongest matches; an exact alias
  normally returns one match. An explicit limit permits broader results.
- `facets`: defaults to false; when true, include namespace/module/family counts
  for broad-search narrowing

Each hit includes purpose, use/avoid conditions, invocation and effect semantics,
and a title-stripped input JSON Schema that retains validation constraints.
The structured record also retains its former `input_shape` field for callers.
Call a selected tool directly (or via `call_tool` for indirect tools) when the
hit supplies enough information; `read_tool` remains available for missing
contract details. Facets are
available when the model needs narrowing statistics, but they should not be
returned by default.

## Artifact Tool Boundary

Artifact tools accept `artifact_id`, not arbitrary local paths.

`artifact_grep` searches existing text-like representations only. It does not inspect image pixels, perform OCR, or create audio transcripts. If an artifact needs OCR, ASR, PDF parsing, or image processing, Pal must discover a suitable capability for that representation or path.

Image import and browser screenshots register artifacts regardless of the
selected model's vision support. Core may attach their pixels within its image
budget when the endpoint supports vision. Otherwise the artifact reference is
available, but Pal cannot claim to have inspected pixels. `browser_navigate`
opens a page and returns its text, metadata, and links in the same call;
`browser_read` can reread the current page after interaction.

## MCP Tool Boundary

MCP tools are not resident by default. The MCP manager plugin compiles discovered server tools into Pal-native capabilities and publishes them into the capability inventory.

MCP prompt templates become declared skills plus render capabilities. They do not enter the resident prompt automatically.

## Invariants

- The resident tool set is exactly the set of `DIRECT` descriptors; nothing else enters the LLM tool window.
- All `INDIRECT` capabilities remain discoverable through execution discovery.
- Tool exposure is not capability availability. Availability is runtime state and must be inspected when it matters.
- External protocol surfaces such as MCP must not bypass Pal execution, approval, or capability policy.

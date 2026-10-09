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
rank first, with case-sensitive exact lookup ahead of case-folded matches. Search
matches complete words, including words separated by underscores in aliases; `install`
does not match `uninstall`. Broad or unmatched queries can be refined using
the result's hit and filter guidance.

Discovery vocabulary is declared in `ToolGuidance.search_terms`: reviewed action and
object synonyms, not arbitrary prose. Existing `search_objects` declarations remain
supported. `search_enum_fields=("operation",)` explicitly includes string enum values
from the selected input property, including local schema references. Unselected enums
(such as output format) do not contribute. All query words must match alias words or
this vocabulary. No fuzzy stemming, global action equivalence or negative-use prose is
indexed. For an empty result, shorten to the domain/object and inspect the tool's schema.
Discovery terms are hidden from normal model surfaces and included in the registry
fingerprint. They survive scoped worker contract serialization.

Important arguments:

- `query`: short whole-word search terms or an exact capability alias
- `namespace`: `inspect`/`introspection`, or `action`/`operation`. These select
  registry namespaces; read-only tools can also be registered under operation.
- `family`: optional family filter
- `module_name`: optional module filter
- `tags`: optional tag filters
- `top_k` / `limit`: hit count, default up to 3 strongest matches; an exact alias
  normally returns one match. `top_k` takes precedence when both are supplied.
  An explicit limit permits broader results.
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

`read_tool(name=..., view="input")` is the default compact contract view.
Use `view="output"` for the output schema, or `view="full"` for
both schemas and the available input example. Output validation failures retain a bounded diagnostic
and offer the output view. Inspect that contract and any retained result before
repeating a command that may already have applied its effect.

## Public Alias Migration

[tool_alias_migration.json](tool_alias_migration.json) records the fixed alias
renames from the tool contract review. Each capability has one public alias;
old names are not compatibility aliases. Update external callers and custom
role allowlists using the table. Canonical capability paths and Python handler
names remain stable. Entries describe the reviewed name migration and do not
imply every capability is registered in every runtime or role.

Native shell also exposes eight independent indirect capabilities:
`read_shell_session`, `write_shell_session`, `resize_shell_session`,
`terminate_shell_session`, `release_shell_session`, `watch_shell_session`,
`extend_shell_session` and `unwatch_shell_session`. Each has its own canonical
path and strict input schema. Read/release retain the exclusive
`session_id`/`output_ref` branches; an output reference cannot wait.
`manage_shell_session` remains a distinct compatibility multiplexer.

All session actions conservatively declare control and reconcile-first retry:
reading also participates in output delivery and acknowledgement. A read alias
does not promise a side-effect-free read or authorize automatic control replay.
Role projections expose these actions only when shell or shell evidence is allowed;
session ownership, pending-output gates and delivery leases remain with the same owner.
Successful empty, unhealthy, unknown and no-op observations are explained in stable
contracts or conditional results; failure guidance is not appended to every success.

Apply the Pal and native shell source changes together. The native shell uses
Pal's shared diagnostic helper. Resident core/execution changes require a full
host restart; copying files or rescanning metadata alone does not activate them.

## Artifact Tool Boundary

Artifact tools accept `artifact_id`, not arbitrary local paths.

`grep_artifact` searches existing text-like representations only. It does not inspect image pixels, perform OCR, or create audio transcripts. If an artifact needs OCR, ASR, PDF parsing, or image processing, Pal must discover a suitable capability for that representation or path.

Image import and browser screenshots register artifacts regardless of the
selected model's vision support. Core may attach their pixels within its image
budget when the endpoint supports vision. Otherwise the artifact reference is
available, but Pal cannot claim to have inspected pixels. `navigate_browser`
opens a page and returns its text, metadata, and links in the same call;
`read_browser_page` can reread the current page after interaction.
The inline text is a preview. The complete captured text is saved as an immutable
`text_file` in the receiving runtime's result store, including brokered worker
reads. Use `rg` and `read_file` on that snapshot for remaining text; reread the
browser only for fresh content. Storage failures retain the preview and report
that the full text is unavailable.
Truncated `capture_browser_snapshot`, `find_browser_text`, and `evaluate_browser_script` results use
the same snapshot storage. Evaluate saves complete JSON for objects and arrays;
reading omitted output never requires executing the script again. Element refs
in a saved snapshot can become stale after page changes.

PDFs retain their page index and per-page file paths when an image is attached.
The prompt states which page is visible; it does not imply all pages were viewed.
The index includes rendered image paths when available, and `has_text=false`
means extraction found no text, not that the page is visually blank.

## MCP Tool Boundary

MCP tools are not resident by default. The MCP manager plugin compiles discovered server tools into Pal-native capabilities and publishes them into the capability inventory.

MCP prompt templates become declared skills plus render capabilities. They do not enter the resident prompt automatically.

Dynamic aliases use `call_mcp_<server>_<tool>` and
`render_mcp_<server>_<prompt>`. Names over 64 characters retain a prefix and a
stable hash suffix. Colliding normalized identities receive distinct deterministic
hash suffixes; original server/tool/prompt identities still route to the external service.

## Invariants

- The resident tool set is exactly the set of `DIRECT` descriptors; nothing else enters the LLM tool window.
- All `INDIRECT` capabilities remain discoverable through execution discovery.
- Tool exposure is not capability availability. Availability is runtime state and must be inspected when it matters.
- External protocol surfaces such as MCP must not bypass Pal execution, approval, or capability policy.

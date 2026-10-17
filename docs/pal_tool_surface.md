# Pal Tool Surface

`ToolSurface` projects registered capabilities into the LLM tool window and selects the reduced tool surface for failure-recovery turns.

Pal may register many capabilities, but only the direct set and explicit native-function opt-ins enter the LLM tool window. Everything else remains in the execution inventory and is discoverable through capability search.

## Source Of Truth

The resident surface is fully determined by capability descriptors at registry compile time:

- Every `CapabilityDescriptor` declares `invocation_mode`: `DIRECT` or `INDIRECT`.
- `DIRECT` descriptors are compiled into the registry generation's `provider_specs` and exposed to the LLM as function-calling tools.
- Owners can opt an `INDIRECT` descriptor into the native tool window with `metadata.native_function=True`. Its original routing, effects, retry policy, admission and permission checks remain in force; model execution dispatches the published function to that record. Low-level direct invocation remains strict. Native schemas inline local nested references so object types are explicit; validation keeps the original schema. Invalid native arguments return the complete input contract, without coercing JSON strings into objects.
- Other `INDIRECT` descriptors stay out of the tool window. They remain discoverable through `search_tools` and invocable through `call_tool`. Use `read_tool` only when the search hit or context lacks contract details needed for a correct call.

There is no tool-surface config file or general tool-surface refresh command.
Changing direct exposure is a descriptor change in the owning module. Plugin
changes publish a new registry generation through the owning lifecycle operation;
resident implementation changes require a Pal restart.

Next-tool hints are rendered against that generation's visible aliases. Missing
optional or role-scoped targets are omitted from model descriptions, without a
suggestion to search for an unavailable alias. The authored relation stays on the
descriptor and becomes visible when the target is registered again. An unknown
hint on a non-detachable first-party tool remains a declaration error unless its
descriptor explicitly permits missing hints.

Model memory CRUD uses `recall_memory`, `remember_memory`, `update_memory`, and
`forget_memory`, with all four complete schemas visible in the stable tool window.
`update_memory(mem_ref, content)` replaces the current summary and retrieval/origin
text together, retaining revision history. Optional topics and full case STAR
remain available; backend adapters still support internal partial corrections.
Provider recall/write/update/delete adapters remain internal;
their duplicate `*_provider_memory` aliases are not published for discovery or
model invocation. Provider maintenance, inventory, lifecycle and index recovery
use the official `pal memory` CLI; `inspect_memory_state` provides the model-facing
hub snapshot. Normal recall uses the active provider. For mutation reconciliation,
`recall_memory(provider_id=<original provider>, mem_ref=<exact ref>)` preserves the
original store without switching the active provider or falling back to another.

`scripts/benchmark_memory_tasks.py` evaluates twelve fixed Chinese memory tasks
with repeated real-model calls and disposable SQLite stores. Its reports separate
tool decisions, schema validity, accepted execution, expected provider failures,
no-op requests, stored outcomes, and preservation of other records. The six held-out
tasks are curated examples, not a representative traffic sample. Tool discovery
uses the authored resident union; only fixture memory handlers execute. The broader
`scripts/benchmark_full_discovery_flow.py` still sweeps every authored alias and
invocation mode, including negative tasks and discovery recovery probes.

The final GLM 5.3 `low` evaluation selected the correct tool and declared mode
for 193/193 aliases in its then-exported catalog, rejected substitution in 10/10 negative tasks,
and recovered in 4/4 probes. First-search recall was 157/193 (81.3%); the first
two searches covered 168/193 (87.0%). The nine selected operation-boundary and
negative cases also passed three repeats (27/27). Full-catalog selection does
not execute business handlers or reconstruct a live role's permissions.

The 2026-10-13 inventory audit found that this historical catalog omitted nine
comprehension-generated verifier aliases. Those role-only tools have now been
added to the exporter and flow catalog. The historical model report remains
unchanged: its 193 successful selections do not establish results for the full
202-alias source union. The nine added aliases have not been model-evaluated in
this audit.

Two consecutive memory evaluations (12 Chinese tasks × 3 repetitions) achieved
36/36 verified outcomes. The final run had one corrected provider-inventory
argument rejection, compared with 18 on the preceding indirect surface; factual
corrections improved from 4/6 to 6/6. A first native-schema pass regressed on
nested STAR objects; inline local references and full input-contract feedback
resolved those fixture cases. Final memory tool windows remained stable and
reported cache hits. These curated cases were inspected during optimization,
so the results do not establish independent production reliability.

Evaluation records: `test-logs/tool-discovery-native-final-evaluation.json` and
`test-logs/memory-native-schema-evaluation.json`. Frozen query replay found no
ordering or domain-visibility violations over 496 registry permutations.
Production Pal was not activated.

Configuration writes for web search backends, core cache reminders and Bunshin
profile/family overrides are available through `pal web-search`,
`pal core cache-reminder`, and `pal bunshin catalog/profile/family`, guided by
`pal.self.maintenance`. Their eleven former write aliases are not published.
Health/auth inspection, cache deadline observation and catalog discovery remain
model tools; plugin reload and channel reconnect remain live owner operations.
Core's configuration CLI uses a narrow resident request endpoint outside the
conversation queue. Online owner failure never falls back to an offline write.

## Object-Scoped Inspection And Unified Source Inventory

Inspection is organized by object, not by individual fields. A hub inspection
returns the hub's own lifecycle, selection, workload and availability information.
Directories return discovery summaries. A named object inspection returns that
object's configuration/lifecycle, health, latest errors and related diagnostics
in one call; it does not recursively expand other objects. The small Web Search
and LLM catalogs use one parameterless snapshot containing all configured search
providers or loaded enabled model endpoints respectively.

Independent diagnostic sections preserve known information if another source
fails. `diagnostics` records the observation time, owner/source, partial coverage
and unavailable sections. Read success is separate from object health. Unknown
or unavailable information is explicit; absence of an error does not prove a
healthy service. Large histories and content remain separate business queries.

The earlier inspection consolidation retired 17 aliases and added two: `inspect_channel_state`
and `inspect_plugin`. Including the subsequent 2026-10-15 tool consolidation, the retained entrypoints are:

| Owner | Hub/runtime inspection | Named object inspection |
| --- | --- | --- |
| Channel | `inspect_channel_state` | `inspect_channel_endpoint(name)` combines lifecycle/auth/health/backlog. |
| Web search | Parameterless `inspect_web_search` includes selection and all providers' public metadata/auth/last-request health. | Included in the same fixed snapshot; no selector or pagination. |
| Memory | `inspect_memory_state` includes L1/L2, active provider availability and bounded record/index counts. | Backend lifecycle, detailed indexes and embedding maintenance use `pal memory` CLI. |
| LLM | Parameterless `inspect_llm` includes selection, current thinking and process usage summary. | All loaded enabled endpoints are included, with one `(active)` label; capabilities are not repeated in a separate active object. |
| Plugins | `inspect_plugin()` inspects the host; `list_plugins` contains summaries. | `inspect_plugin(name)` includes generation/dependencies/cleanup/installation and errors. |
| LSP | `inspect_lsp_status()` passively inspects the host. | Supply `workspace_root` for recorded readiness; add `name` or `file` to actively diagnose only the selected server. |
| MCP | `inspect_mcp_state` inspects the host. | `read_mcp_server(name)` inspects one server and its discovery snapshot. |
| Proactive | `inspect_proactive_status` inspects the scheduler/runner. | `read_proactive_task(name)` includes configuration and latest execution outcome; `list_proactive_runs` retains history. |
| Artifact | `inspect_artifact_state` includes policy and caller-scoped resource counts. | `inspect_artifact_info` includes representations and hot/hard expiry information. |

Core workload and cache deadlines share `observe_core`; the mode-label setter is
internal. `read_checklist` is the single checklist snapshot. File-cache diagnostics
are internal; file edit failures retain the read-before-edit recovery path.
Parameterless `inspect_failure` combines counts and the latest 16 bounded report
summaries. Complete returned reports and evidence use one read-only result snapshot
file with L1 ownership; a failed save retains summaries and marks details unavailable.
Skill hub inspection bounds recent pending candidate summaries.
Browser status includes configured-extension summaries or one selected extension; page/layout/network business operations remain separate.

LSP workspace and MCP server inspection do not start a sidecar. Host inspections
retain passive process/errors/cached summaries when the RPC owner is unavailable.
Active LSP diagnosis requires an explicit server selector in `inspect_lsp_status`. Inspection may still perform
its established bookkeeping (artifact hot-state refresh); this consolidation does
not widen write authority or change artifact retention rules.

Bunshin workflow control uses `control_bunshin_workflow(task?, operation, reason?)`
with pause/resume/cancel/archive; lifecycle state checks stay with the existing
owners. Delivery rebind is indirect; start remains direct. Restarting an execution
attempt and resolving a specific blocker remain separate tools.

MCP server connection control uses `manage_mcp_server(name, operation=attach|detach)`.
Artifact inputs are resolved inside the external-tool adapter using local
`artifact_inputs` parameter mappings and the calling conversation's visibility;
there is no standalone `prepare_mcp_image` tool.

Skill inspection, search, read and injection remain separate. Skill disabling
uses `update_skill` with `patch={"status":"disabled","enabled":false}`;
reactivation uses `patch={"status":"active","enabled":true}`. Both use the
normal content-update file/database/version path. The old `disable_skill` tool
is withdrawn.

Proactive task control uses `manage_proactive_task(name, operation)` with
`enable`, `disable` or `delete`. Disable retains the task and run history; delete
removes its definition. Both parameters are required.

Proactive destination changes use `set_proactive_destination`: omit channel to
retain it, supply a channel to resolve a new destination, or pass explicit null
to clear output. A supplied target is validated against the selected channel;
`{}` deliberately clears only the target. A task name alone is rejected. The
channel and target are persisted together; schedule updates remain independent.

Control and identity expose no LLM tools. `pal control status` reads the live human
control registry. `pal identity show` reads the resident projection online, or
persisted identity offline. `pal identity refresh` explicitly refreshes the live
projection without sending a conversation. Owner failure never falls back to
storage writes. Language/tone/preferences apply to subsequent prompt assembly;
system identity name and core policy retain their existing startup-pinned behavior,
which the CLI reports as `startup_pinned_fields`.

Count public aliases once and separate the main Pal source union from role-only
tools. After the Proactive, Skill, MCP, Bunshin, Artifact, Web Search, LLM and
Failure consolidations, the 2026-10-15 declaration audit confirms **145 distinct
aliases**. Declaration imports and AST exposure classification were checked without
constructing a runtime. Compiled-registry behavior and regression tests remain
deferred to the next module batch:

- **109** belong to the main Pal authored union: **19 native** (16 direct plus
  three indirect native opt-ins) and **90 search-only**. Bunshin delivery rebind
  no longer occupies a native slot.
- **36** belong to the separate Bunshin role union. Each live role receives its
  allowed subset; these tools do not increase the main Pal count.
- Unpublished backend adapters, external MCP tools and installed third-party
  contributions are excluded. This is not an online inventory.

| Owning module | Main aliases | Native | Search-only |
| --- | ---: | ---: | ---: |
| Artifact | 4 | 0 | 4 |
| Browser | 23 | 1 | 22 |
| Bunshin | 11 | 1 | 10 |
| Channel | 7 | 0 | 7 |
| Checklist | 4 | 3 | 1 |
| Control | 0 | 0 | 0 |
| Core | 1 | 0 | 1 |
| Emotion | 1 | 1 | 0 |
| Execution | 8 | 7 | 1 |
| Failure | 1 | 0 | 1 |
| Identity | 0 | 0 | 0 |
| LLM | 1 | 0 | 1 |
| LSP | 10 | 0 | 10 |
| MCP | 5 | 0 | 5 |
| Memory, including L3 | 8 | 4 | 4 |
| Plugins, including packages | 8 | 0 | 8 |
| Proactive | 8 | 1 | 7 |
| Skill | 7 | 0 | 7 |
| Web search | 2 | 1 | 1 |
| **Main Pal union** | **109** | **19** | **90** |

This inventory imports definitions without constructing a runtime or executing
tools. Actual visibility depends on mounted owners, targets, permissions, dynamic
MCP tools and external packages. It is not the installed or running registry.
Failure turns use a smaller surface. This inventory audit does not run model
evaluations; later sections report discovery trials on the consolidated catalog.
Production activation requires separate handling of resident code and plugins.

`docs/tool_surface_review.json` records current decisions and retired-alias
replacements. Export one row per current alias, with sources and visibility:

```sh
python scripts/audit_tool_surface.py --output test-logs/tool-surface-consolidated-audit.json --csv test-logs/tool-surface-consolidated-audit.csv
```

Counts are recalculated from current source. Generated-role source locations
point to the assembled worker catalog; `generated_spec_key` identifies the
evaluated spec rather than a nonexistent literal alias declaration.

## Failure Surface

`select_failure_descriptors` builds a small, subsystem-scoped tool set for failure-recovery turns (for example memory provider introspection when the failing subsystem is `memory`). This selection lives in code, is keyed by canonical paths, and is independent of the normal-turn surface.

## Discovery Schema

`search_tools` is the resident discovery entry point. Use `domain` alone to browse
a bounded directory, or `query` to rank contracts. Exact alias queries use
`query`, not `name`.

Search with English alias words in `[domain] [action] [object]` form, such as
`remember memory`, `lsp incoming calls`, or `browser screenshot`. Exact aliases
rank first, with case-sensitive exact lookup ahead of case-folded matches. Search
matches complete words, including words separated by underscores in aliases; `install`
does not match `uninstall`. Broad or unmatched queries can be refined using
the result's hit and filter guidance.

Plugins can declare operation ownership with `ToolGuidance.search_domain`, a
lowercase owner name, independently of their implementation module. Optional
`search_domain_aliases` accept up to eight lowercase abbreviations or full names;
aliases route to the same strict bucket and cannot claim another owner's name.
Conflicts reject the candidate registry generation. `search_domain_description`
provides a short explanation (up to 160 characters) for the live domain list in
the system tool policy. Domain metadata contributes to the registry fingerprint
but stays out of retrieved contracts. Without an explicit declaration, existing
alias and module ownership rules apply. For example, a native execution plugin
can declare `shell` for command/session tools and `remote` for target lifecycle
tools while continuing to mount under the execution module.

Discovery vocabulary is declared in `ToolGuidance.search_terms`: reviewed action and
object synonyms, not arbitrary prose. Existing `search_objects` declarations remain
supported. `search_enum_fields=("operation",)` explicitly includes string enum values
from the selected input property, including local schema references. Unselected enums
(such as output format) do not contribute. Positive `purpose` clauses also contribute
lexical recall and binary-term BM25 relevance. Alias/action/object coverage strengthens ranking; exact aliases remain
first. Existing purpose recall keeps its two-word and 60% coverage checks. An
additional partial-match path accepts supported action/object anchors even when
structured vocabulary already supplies all the useful evidence. Diagnostic
context such as manager, runtime, backend and configuration may remain unmatched;
it lowers rank rather than excluding an anchored candidate. Without an explicit
domain, declared object evidence or two purpose object words are still required.
Unknown substantive objects, unsupported actions, inactive conditional synonyms
and negation continue to block partial admission. Operational constraints such as
encrypted, remote and authentication are not generic context modifiers.
Complete structured matches retain priority. Partial hits expose `matched_terms`
and `unmatched_terms`; the latter never establishes feature support. Review the
whole request against the contract before execution. This uses lexical evidence
and bounded vocabulary, with no vector service.
A `without ...` suffix is excluded while the preceding affirmative operation stays
indexed. Uppercase state constants (such as `TRIAGE_REQUIRED`) contribute words;
lowercase tool-alias references do not. Compound purpose actions contribute only
when the query also names the main operation, so `enable attach plugin` can match
an enable-and-attach tool while `browser fetch` does not imply network observation.
Negative clauses, use/avoid examples and recovery prose are not indexed. No vector index or embedding service is used. An empty result does not
establish that a capability is unavailable.
Shorten to the domain/object and remove guessed filters. If it still misses,
browse the appropriate `search_tools(domain=...)` directory before falling back; use `read_tool` for a known alias.
Discovery terms are hidden from normal model surfaces and included in the registry
fingerprint. They survive scoped worker contract serialization.

Review common task queries against each tool's actual operation before adding
synonyms to its owning guidance. Keep lifecycle operations distinct: rescan does
not reload code, reconnect does not reload a provider, and pausing a scheduled
task does not delete it. Run `tests/test_tool_discovery_vocabulary.py` to check
conventional queries, exact aliases, and operation boundaries across the source catalog.
Check the entire returned alias set, including explicit `top_k=5`, rather than
only asserting that the expected alias appears. Inspection words such as `check`
must not route general state queries to completion or checkbox setters.

Bind ambiguous synonyms to explicit query objects using `search_term_rules`:
`terms=("check",)` with `requires_any=("checkbox", "checkboxes", "radio")` enables
checking controls without matching general browser-state inspection. Override reset
accepts `remove`/`clear`/`delete` only with `override`/`overrides`. Conditions are
whole words from the original query, must already be indexed, and never recursively
activate other rules. Conditional terms cannot also be indexed unconditionally
through alias words, ordinary vocabulary, or selected enums.

For query-only broad discovery, `search_scope_terms` declares a narrower scope such as `provider`. Among otherwise
equal matches, a tool whose extra scope was not requested ranks below a general
entry point. Exact aliases remain first; explicit `top_k` can include weaker matches.
Scope terms influence ranking only and do not add searchable words.

The system Tool Policy explains public action-first aliases and recommends an
operation-owner `domain` plus a task description in `query`. Domain searches do
not require action/object naming conventions. The System Map names
search domains alongside subsystem responsibilities; these keywords are not
`module_name` or `family` filters and do not promise that a capability is mounted.
Public lifecycle names matter: internal plugin `reattach` is exposed as
`manage_plugin(operation="reload")`; reload, restart, attach and rescan have distinct contracts.

For query-only broad discovery, when no complete query match exists, `suggestions` can contain up to three fuzzy
matches (bounded by a smaller `top_k`). Each must match at least two query words,
including a declared object, and miss exactly one word. The query must contain a
recognized public domain, and every requested domain must occur in the candidate
alias. Domains are never silently spell-corrected or discarded. The unmatched word
must have a listed recovery equivalent in the candidate vocabulary, be a browser
page qualifier, or be an unknown word within one edit of a candidate word (minimum
four characters, adjacent transpositions included). Known words cannot be treated
as spelling errors. Arbitrary unknown constraints are never simply dropped.
These recovery alternatives are not added to strict vocabulary. Conditions and
explicit filters still apply; filtered complete matches
retain `filter_suggestions` instead. Suggestions expose alias, purpose, matched and
unmatched query words, not invocation arguments, and do not enter hit counts.
Use `read_tool` to verify the original request, including the unmatched word, before
selecting a suggestion. Unknown words may carry essential constraints.

`search_tools.domain` optionally selects a strict operation-owner bucket.
File read/edit/write/delete tools belong to `file`, even though their implementation lives
in the execution module. Declared
abbreviations/full names normalize identically (`llm` / `large language model`,
`lsp` / `language server protocol`, `mcp` / `model context protocol`). Hits expose
their canonical `domain`. Existing module ownership assigns the bucket, with
explicit file/shell exceptions for tools hosted by execution. The assignment is
compiled into the immutable registry and included in its fingerprint. Provider
fallbacks use unambiguous alias domains or the provider's module name.

An explicit domain limits both complete hits and fuzzy suggestions, including
exact aliases. Unknown/unavailable domains return no hits and list the current
generation's `available_domains`; they never silently select another bucket.
Other filters still intersect the domain, including BM25 corpus statistics. Query-only
search does not infer a hard bucket from target names (e.g. `lsp` in a
plugin-management request). Existing exact and conservative keyword hits retain
their priority and order. If they return no hits, purpose retrieval searches all
visible tools intersected with the supplied filters. This fallback requires at
least two distinct positive term matches; it does not require full query coverage
or gate admission on action/object words. It returns at most the same three full
contracts by default, without padding existing keyword hits. Unknown or incorrect
explicit domains never use this cross-domain fallback.

When query words are uncertain, `search_tools(domain=owner)` returns
a directory before keyword matching. It contains at most 16 visible aliases and
purposes capped at 160 characters, with `purpose_truncated=true` when shortened.
No per-tool input schema or full usage guidance is included in directory entries.
Read a plausible full contract; an exposed native schema already provides it.
With an explicit domain and query, BM25 ranks positive `purpose`, tool-owned
`search_terms`, selected operation enum words and `search_intents` together.
`search_objects`, alias action/object words and scope metadata do not gate or boost
these candidates. Compiled `search_auxiliary_terms` separates authored terms/enums
from object-only vocabulary. Exact aliases retain direct lookup under all filters;
a known alias excluded by filters is not replaced by a lexical neighbour.
The default result budget is three full contracts; explicit `top_k`/`limit` allow
one through five. Positive lexical overlap can admit a partial candidate;
unmatched query words are disclosed instead of blocking admission. Zero positive
evidence returns no hits. Noun number is normalized without business synonyms.
Queries should describe the desired operation briefly in English, matching the
primary language of tool purposes. Excluded alternatives and original constraints
remain part of contract review rather than being mixed into lexical query terms.
Only after a lexical miss, bounded one-edit spelling recovery can return up to
three `suggestions` (or fewer with a smaller `top_k`). Each
suggestion includes its full `contract`, internal retrieval score/source,
`matched_terms` and `unmatched_terms`. These are candidates, not confirmed matches
or probabilities of capability support. Compare the included contract with the
original task and invoke directly when suitable; do not reread a complete contract.
Requested actions, negation and qualifiers can remain unmatched; lexical
retrieval does not establish that they are supported. Full contracts and the
original task govern final selection. Conditional auxiliary words activate only
from the original query. Query-only broad discovery preserves the conservative
keyword and reciprocal rank fusion stage. Purpose fallback may replace suggestions
when that stage has no hits; otherwise spelling recovery remains available.
`search_intents` are internal declaration metadata, bounded to
eight unique phrases of 160 characters per tool, frozen with the registry and
included in its fingerprint. They do not enter native tool descriptions or public
search results. No embeddings, remote
retrieval service or additional LLM inference is involved.

When neither hits nor suggestions are available, keyword misses return the compact
directory in the selected, available domain. Explicit directory browsing always
returns the directory without running retrieval. Retrieval and model selection
are evaluated separately: first full-contract Recall@k/MRR includes both hits and
contract-bearing suggestions; strict-hit recall and directory visibility retain
their distinct meanings. See `scripts/benchmark_fused_discovery.py` for paired
selection, round-count and token evaluation against a frozen source/catalog/prompt.
`scripts/benchmark_domain_purpose.py` compares explicit-domain purpose retrieval
with a frozen matcher using the same current catalog, schema and task-description
prompt, then reports frozen-query recall separately from fresh model selection.

The domain-purpose baseline replayed 191 positive samples covering all 187
authored aliases plus probes: first full-contract top-3 recall rose from 164/191
to 174/191. Four of ten negative first queries gained candidates. All 187 exact
aliases remained top-1; 247 registry permutations preserved output with zero
domain violations. In a paired GLM 5.3 trial of 51 distinct positive and ten
negative Chinese tasks, both matchers selected all positive aliases/modes and
refused all negative tasks. With the same **new task-description prompt/schema**,
first-contract recall was 9/51 for the frozen matcher and 40/51 for this baseline;
positive mean rounds fell from 2.88 to 2.18, while negative rounds rose from 3.0
to 3.4. Total reported input tokens fell 14.1%; search response characters rose
47.3%. The shared new prompt isolates the matcher but differs from the old
production prompt; frozen-query recall is the unchanged-query comparison.
An earlier pilot did misselect a plugin uninstaller for absent endpoint deletion;
final negative success is a small-sample outcome, not a guarantee. No business
execution or runtime activation occurred. Reproducible records, source hashes
and limitations are in `test-logs/discovery-domain-purpose/analysis.json`.

Discovery guidance asks the model not to search indefinitely. An unresolved
search can try a reasonable rephrasing, correct a guessed domain/filter or inspect
relevant inventory. If those checks still reveal no suitable callable, the
capability may be unavailable; explain that possibility, the checked scope and
any uncertainty. Reuse unchanged inventory and complete contracts, and use a
suitable contract once found. This guidance appears in the system tool policy,
tool-window guidance, search schema/guidance and result hints. There is no fixed
search-round budget or reset mechanism. Earlier three-round trials remain in
test logs as superseded experiments.

The paired GLM 5.3 lexical-fusion trial used 70 positive runs (51 distinct cases)
and 12 negative runs per variant. Both variants selected all positive tools;
first-search full-contract recall improved from 35/70 to 47/70. Mean rounds across
all cases fell from 2.66 to 2.54 and reported input tokens fell 2.4%. Correct
negative refusals improved from 11/12 to 12/12, but negative cases took more rounds
(3.33 to 3.58). This is a limited alias/mode selection experiment, not business
execution or a production reliability estimate. Records and source snapshots are
in `test-logs/discovery-fused-glm53/`.

Known action/object words use keyword search by default. A paired GLM 5.3 trial
used 38 repeated original positive requests and 16 independent positive requests,
plus eight negative requests per variant. Both variants selected all 54 positive
tools correctly, while directory-first discovery increased mean rounds from
2.39 to 2.50 and reported input tokens per positive case from 9943 to 10917.
Consequently bounded domain browsing remains an option rather than the default
first step for every unfamiliar capability. Input-token counts are not billing
costs; this isolated evaluation validates alias/mode selection, not business
execution. Records are in `test-logs/discovery-domain-directory-glm53/`.
All namespace/module/family/tag filters still apply. This is navigation, not
query recall: entries are not added to hits or suggestions and do not imply that
the original request is supported. Read a plausible tool's contract before
selecting it; retain the original task's constraints.

Use `directory_offset=0` with an explicit domain to browse directly, or follow
`directory.next_offset` using the same domain and filters. Query words are ignored
for explicit directory pages. Ordering is by alias and belongs to the selected
visible registry generation. Without a valid domain, misses return
`available_domains` so the caller can select a bucket explicitly. Directory pages
do not silently cross domains or expose tools outside the visible generation.

Keep network observation indexed by `network`/`api`/`traffic` rather than the
page-fetch action `fetch`.

Export the authored catalog, including selected operation enums, with:

```sh
python scripts/audit_tool_discovery.py --output test-logs/tool-discovery-catalog.csv
```

This imports definition modules without starting Pal or invoking tools. The CSV
includes purpose, use_when, do_not_use_when, failure_next_steps, search_intents and
next_tool_hints so the readable contract can be reviewed beside its search metadata.
It keeps unconditional words in `effective_vocabulary`, query-dependent rules in
`search_term_rules`, and ranking scope words in `search_scope_terms`. It
keeps separate provider declarations and marks Bunshin role-only tools; those
tools do not share the resident tool surface. It describes source contracts, not
the running registry. External MCP tools are discovered dynamically and are
outside this catalog; review their configured guidance overrides separately.

For a known domain, purpose retrieval indexes affirmative `purpose`, authored
auxiliary terms, selected operation enums and `search_intents`; conditional terms
apply only under their authored conditions. `search_objects` also supports the
keyword stage. `use_when`, `do_not_use_when`, failure guidance, next-tool hints and
arbitrary input descriptions are not indexed as capability claims. Their prose
can name alternatives or prerequisites without making those alternatives a tool's
own capability. When a supported intent exists only in `use_when`, express it in
purpose or narrowly scoped auxiliary words/intent phrases too. Select operation
enums explicitly rather than manually keeping duplicate operation words in sync.

Important arguments:

- `query`: short whole-word search terms or an exact capability alias
- `namespace`: `inspect`/`introspection`, or `action`/`operation`. These select
  registry namespaces; read-only tools can also be registered under operation.
- `family`: optional family filter
- `module_name`: optional module filter
- `tags`: optional tag filters
- `top_k` / `limit`: hit count from 1 to 5, default up to 3 strongest matches; an exact alias
  normally returns one match. `top_k` takes precedence when both are supplied.
  An explicit limit permits broader results within the five-hit maximum.
  The public schema rejects larger values; the backend also clamps internal
  callers to five hits. Suggestions remain capped at three. Candidate scoring
  may inspect the visible bucket internally without returning it to the model.
- `facets`: defaults to false; when true, include namespace/module/family counts
  for broad-search narrowing
- `directory_offset`: browse a compact page in an explicit domain; use 0 to
  begin, then `directory.next_offset` to continue. Separate from hit limits.

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
The old `manage_shell_session` multiplexer is no longer a model tool; internal dispatch remains shared.

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

Artifact is a file normalizer and scoped reference owner. Original filenames,
extensions, MIME, processing status and original/processed paths are projected
from L1 references into model input. Only images attach content automatically;
text, PDF pages and audio/transcripts use file paths and task-appropriate tools.
New PDFs have page text/images plus an index, without combined text or chunks;
UTF-8 text can reuse its managed original. Unsupported types retain their source
without pretending to have parsed it.

Four indirect tools remain: import_artifact, search_artifacts, inspect_artifact_info
and inspect_artifact_state. Search without a query lists attachments and reports
pagination explicitly. read_artifact/grep_artifact/select_artifact/list_artifacts
are withdrawn. The select owner method remains available for runtime leases,
including compacted L1 references. Detailed behavior is in [artifact_manager.md](artifact_manager.md).

Image import and browser screenshots register artifacts regardless of the
selected model's vision support. Core may attach their pixels within its image
budget when the endpoint supports vision. Otherwise the artifact reference is
available, but Pal cannot claim to have inspected pixels. `read_browser_page(url=...)`
opens a page and returns text, metadata and links; omit URL to reread the current page after interaction.
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

## Channel endpoint control consolidation (2026-10-15)

Channel now has seven indirect tools. `manage_channel_endpoint(name, operation)`
replaces the five separate attach/detach/restart/enable/disable aliases, which are
not retained for model discovery or invocation. Both arguments are required;
operation is one enum and does not introduce action-specific parameter sets.
Attach preserves an existing transport; restart requests a fresh transport without
reloading provider code. Enable/disable persist acceptance state without replacing
the instance; detach retains endpoint configuration. Existing owner dispatch,
rollback, recovery-socket protection, hub lifetime and lifecycle writer admission
remain in force. The combined entry conservatively declares control/reconcile-first.

Hub inspection, endpoint directory, object inspection, text/attachment sends and
credential configuration keep their existing boundaries. The already-attached
receipt now offers `manage_channel_endpoint(operation=restart)` with the endpoint
name bound. Source implementation does not activate a running host.

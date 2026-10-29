# Tool Affordance Audit (2026-09-23)

> Historical record, archived on 2026-10-05 from `docs/tool_affordance_audit_20260923.md`.
> Statements about implementation, deployment and test results describe the
> original work, not the current checkout or running host. See the
> [current documentation index](../../README.md).

Audit baseline: `26bfb5e`. The original findings are preserved below; implementation results for this round are at the end of the document. The workspace also contains independent compact copy and OR hybrid request compatibility fixes.

## Scope and Evidence

- Statically traversed `src/pal`, `plugins`, and `providers`, extracting 199 `capability_action` declarations from 24 files, examining tool guidance, InputModel, and main return paths.
- Read the real Pydantic schemas for 105 of the centrally generated input models: 304 top-level fields, of which 89 have no field description. This is a screening signal, not 89 defects; ordinary `name` fields and fields already explained by upper-level guidance should not mechanically get copy added.
- The remainder includes independent input models, empty-parameter tools, and dynamic endpoint locator parameters; the absence of an explicit InputModel must not be equated with the model being unable to obtain parameters.
- Deep dives were done on discovery, LLM usage, channel/Telegram, checklist, artifact, browser, and proactive; the remaining modules received declaration-level scans only, and no claim is made that all error branches have been execution-verified.
- Combined with the TG troubleshooting history in `/tmp/pal/pal.log`. That file is a snapshot of the last request, not a complete per-round performance trace.
- Offline calls against real schemas/functions reproduced the discovery filtering error, the browser negative index, and measured the usage snapshot field counts. The browser's underlying execution was replaced with a captured stub; there were no real browser operations, paid calls, or runtime changes.
- Dynamic MCP servers, independently installed `pal-shell-native`, and other repositories are outside this source coverage. Native shell entries in the logs cannot be attributed to the core `run_shell` declaration.

## Confirmed Issues and Recommendations

### A1: search_tools empty results cannot explain filter conflicts (High)

Location: `src/pal/execution/generated_tool_models.py:482`, `src/pal/execution/runtime.py:1057`.

`family` is a free-form string whose examples mention management/lifecycle/endpoint/search, but it does not distinguish it from module. In the TG logs the model passed `family=channel`, while the relevant tools have family introspection. All filters are applied first, and facets are then computed from the filtered results; therefore even with `facets=true`, a zero-hit query only yields three empty arrays.

Offline reproduction: the same channel health record yields 0 results and empty facets with family=channel; removing family yields 1 result with family=introspection.

Recommendation: explain that family is a registry classification and that it should be omitted when unknown; on zero hits, return a conditional filter-correction suggestion along with available classifications, rather than silently relaxing the filter and pretending it was a precise match. family/module are dynamic sets; current values must not be hardcoded as a permanent enum. When `top_k`/`limit` are both provided, the former currently takes precedence; this should also be made explicit.

### A2: llm_usage returns all aggregate detail by default, lacking view selection (High)

Location: `src/pal/llm/capabilities.py:266`, `:383`; `src/pal/llm/usage.py:175`.

The tool takes no input parameters and outputs active_model + usage totals + all endpoint buckets. An empty ledger already has 42 top-level fields, and each endpoint bucket has 39 fields; plus there is active_model. This includes latest-request detail, counting conventions, coverage, reported_fields, anomalies, near-duplicate ratios, and more.

This is an aggregate snapshot, not an unbounded request detail table. Pagination of results already obtained does not solve an inappropriate default information hierarchy.

Recommendation: by default show a process-level overview and data completeness; select endpoint/detail on demand. Preserve the distinction between cost_complete and unknown cost/usage; unknowns must not be represented as zero for brevity. The current ledger has no independent per-turn query contract, so do not promise direct querying of the current turn's cost unless a correct data source is wired in separately.

### A3: Telegram health mixes current polling state with historical bypass errors (High)

Location: `providers/telegram/endpoint.py:873`, `:1381`, `:1396`, `:1999`, `:2010`.

`healthy` is based on polling_running and last_poll_error; the same result also returns last_status_error, which may come from reaction/typing/menu and lacks occurrence time and recovery information. Therefore healthy=true appearing together with Timed out is not a logical contradiction, but the model would have to read the source to know the judgment boundary. This phenomenon has already triggered extra investigation in the TG logs.

Recommendation: return the scope of the health judgment, the error's operation type, the error time, and any subsequent successes. "No backlog" currently only means the current queue is empty and cannot rule out past message delays. Leave low-level observation metadata to the harness; expose only what the model needs to understand the judgment.

### A4: checklist UI echo enters LLM output in full, and the wrap-up contract is overly serialized (High)

Location: `src/pal/checklist/capabilities.py:135`, `:205`, `src/pal/checklist/prompt.py:40`.

The same result redundantly contains plan/markdown/echo.payload.plan/echo.markdown. In the TG instance, a single checkbox tick is about 1.1K characters, and the last three checks plus clear took four model rounds.

Recommendation: keep the UI echo for channel/harness, while the LLM by default sees only state changes and necessary next steps; the copy should make clear that the whole plan can be updated in batch and that clearing does not require ticking items one by one at task end. There is no need to create a larger checklist framework, nor to let the runtime guess on its own that the task is complete.

### A5: artifact recommendations cite wrong public aliases (High)

Location: `src/pal/artifact/service.py:1195`; `src/pal/execution/generated_tool_models.py:32`, etc.

The PDF next_actions recommend calling `artifact_read`, but the public alias is actually `read_artifact`; multiple artifact_id descriptions say to obtain IDs from `artifact_search`, but the public alias is actually `search_artifacts`. An internal method with the same name is not equivalent to a callable tool alias.

Recommendation: uniformly use the real public aliases and add a contract check for registry references. There is no need to add a discovery flow to mask bad suggestions.

### A6: search_artifacts.time_hint is accepted but entirely ignored (High)

Location: `src/pal/execution/generated_tool_models.py:59`, `src/pal/artifact/service.py:534`.

The schema exposes a string time_hint defaulting to recent; the implementation simply does `_ = time_hint` and actually only uses fixed hot-state validity and recency weighting. Passing values like last_week produces no corresponding filtering.

Recommendation: remove/reject the ineffective parameter explicitly, or define the actually supported time semantics and then implement it; do not wrap a still-nonfunctional parameter in an enum.

### A7: browser_tabs negative index silently points to tab 0 (High)

Location: `src/pal/web_fetch/tool_models.py:111`, `src/pal/web_fetch/browser_service.py:648`.

index has no range/numbering explanation; the schema accepts -1, and dispatch uses max(0,index). Reproduced offline via the real schema and dispatch: `{operation:close,index:-1}` generates `tab-close -- 0`. This is not just a wasted-round risk; it also selects the wrong operation target.

Recommendation: explain that the index comes from the tabs list, add ge=0 and make invalid indices error; explain the meaning of an omitted index for select/close, deciding whether an explicit value is required based on the underlying CLI's real contract. Do not guess the target on the user's behalf.

### A8: read_artifact page/chunk numbering and their relationship are unexplained (Medium)

Location: `src/pal/execution/generated_tool_models.py:42`, `src/pal/artifact/service.py:881`, `src/pal/artifact/processors.py:436`.

page/chunk are optional integers with no description and no lower bound; chunking actually starts at 1. When both are passed, `_select_representation` prioritizes page, which the schema does not state. max_chars is also unexplained relative to tool-result pagination.

Recommendation: explain the numbering source, starting point, and applicable representation, and reject or explicitly handle mutually exclusive combinations; do not conflate tool-result pagination's page with artifact page numbers. The existing 1-based/head/tail documentation of `read_tool_result` is a good example to follow.

### A9: proactive output routing parameters have an unreliable discovery path (Medium)

Location: `src/pal/execution/generated_tool_models.py:1184`, `:1227`; `providers/telegram/endpoint.py:913`.

The out_reply_target description says to query the endpoint's auth_state to obtain session_id/request_id. But Telegram's auth_state only has paired/authorized/token_present; its reply_target uses fields like chat_id/message_id/thread_id. The generic tool description writes one channel's fields as a universal contract.

Suggestion: return available destinations via channel owner or provide an explicit provider routing schema; the model should not guess internal routing fields. schedule currently has an example, but it is still an arbitrary dict; in the future, conditionally required fields could be expressed via a cadence-based input structure.

### A10: browser target/key-value parameters lack sufficiently specific syntax documentation (Medium)

Location: `src/pal/web_fetch/tool_models.py:45`–`:81`, `:111`, and `src/pal/web_fetch/capabilities.py:258` onward.

target is only a str; guidance says current snapshot ref or unique locator, but provides no allowed locator syntax/examples. The valid forms of key, modifiers, and select.value are not documented in the fields either. The model usually has to read a separate manual or guess CLI syntax from experience.

Suggestion: use verified enums for static sets; for open expressions, give examples, sources, and invalidation boundaries; do not casually mix CSS, Playwright locator, or other CLI syntaxes. Ref changes after a browser new page should be conveyed by the returned affordance, not by repeatedly sending generic operation manuals.

## Things That Must Not Be Mistakenly Changed

- `call_tool` already states clearly: a known accurate alias plus a sufficient contract is enough to call, without requiring read_tool every time. TG's extra documentation reads that time also had model-policy factors.
- discovery's input_shape is a summary, not a complete schema. You cannot therefore claim all hits are unconditionally callable; omit read_tool only when existing information is genuinely sufficient.
- File tools already have authorization snapshots, batch edits, and pagination; large-scope reads first check call parameters, and evidence must not be hard-truncated again.
- memory's kind/view etc. already have enums and fairly complete descriptions; the pagination capability must not regress to silently dropping content by character count.
- Bunshin's task/subject, workflow command, etc. already have semantic positioning and some explicit enums; this round's claim scan did not prove a full refactor is needed.
- `read_tool` not showing the full output_schema by default is an existing noise-reduction choice. What needs supplementing is key result semantics; the full schema must not simply be stuffed back into every tool result.
- When a parameter's configurable values come from a provider/MCP/plugin, do not force an incomplete global enum.

## Suggested Implementation Order

1. Fix contracts that are wrong or can cause wrong target selection: artifact aliases, invalid time_hint, browser negative indices, proactive routing documentation.
2. Fix high-frequency result expressions: llm_usage default summary/on-demand detail, channel error timing and semantics, checklist LLM/UI separation.
3. Fix discovery and input affordances: empty-result filter suggestions, parameter numbering/conflicts/syntax, missing dynamic capability entry points.
4. Regressions should use "model-visible schema → actual validation/invocation → model-visible result" as the unit; do not write tests that only verify the description text is non-empty. Add negative cases: wrong next-tool alias, silently ignored parameters, invalid targets automatically rewritten.

This report is an audit checklist; it does not mean all suggestions have been implemented, nor does it attribute all of TG's rounds to tools. It did not change existing pagination, execution permissions, or tool lifecycles.


## Implementation Results This Round

- A1: Preserved exact filter semantics; on zero hits, returns `filter_suggestions` based on query-matched items, separate from actual hits/facets. Added family/module distinction and top_k priority.
- A2: `llm_usage(view="summary"|"detail", endpoint_id=...)`. By default, returns only the process overview and usage/cost completeness; details and endpoint buckets are read on demand. A missing endpoint record clearly returns no_recorded_usage, without constructing zero-cost results. The original ledger and user status control commands retain full data.
- A3: Telegram health now clearly states the judgment scope of current inbound polling; recent auxiliary errors include the operation, UTC failure time, and the time of a later success for the same operation. Historical errors are retained; another operation's success is not treated as recovery; errors are logged to the local logger.
- A4: checklist's model-facing update results keep only changes and progress; full snapshots and echo remain in structured for UI use. upsert can update multiple statuses at once; when the final check completes all items, clear is reused inside the service lock, returning a full completion snapshot and sending the completion echo only once; batch upsert keeps its original semantics, and closing after cancel, replace, or batch completion still uses clear. Before the final check, review requirements and actual results, without repeating already-completed checks.
- A5: Fixed the public aliases of read_artifact/search_artifacts; added contract validation for next_actions against registered aliases.
- A6: Removed invalid time_hint from the public schema, tool forwarding, and the service signature; the old parameter is rejected by strict input validation; documentation clarifies it is only relevance/recency ordering, not date filtering.
- A7: Schema and execution layer reject negative tab indices; select must give a number, close without a number still means the current tab, consistent with the actual CLI.
- A8: page/chunk are marked 1-based, passing both is rejected, and artifact page numbers are distinguished from tool-result pagination; documents that max_chars is a text preview budget.
- A9: Removed the erroneous promises that auth_state can provide destinations and generic session_id/request_id. Documents using a known inbound reply_target or existing proactive configuration, with Telegram fields explained separately. This round added no cross-provider destination discovery API and did not restructure the schedule input.
- A10: Based on the locally installed @playwright/cli README and Playwright schema, completed the syntax and invalidation boundaries for ref, CSS/locator, key, and select value; modifiers use a supported enum. No new browser operations or model requests were added.

### Verification and Effectiveness

Relevant regressions: 169 passed (covering Telegram endpoint, browser, artifact, discovery, usage, checklist, and recovery scenarios); the full CI batches and real network/browser operations were not run. Tests for the SDK cache fix are documented separately.

Changes are in the source workspace, not committed or activated. execution/core code requires a reload by the main process; Telegram is a separate provider source, and if the running directory is a copied installation, that copy must be updated first and then channel_reload_provider invoked — merely reconnecting the endpoint will not load the new implementation. No runtime configuration was changed and no working Pal was restarted.

# Pal Tool and Prompt Full-Path Efficiency Audit

> Historical record, archived on 2026-10-05 from `docs/tool_efficiency_audit_20260927.md`.
> Statements about implementation, deployment and test results describe the
> original work, not the current checkout or running host. See the
> [current documentation index](../../README.md).

Audit baseline: `1893d38`. The findings below were recorded at that baseline; subsequent remediation is implemented according to the confirmed decisions at the end of the document. No deployment, no calls to paid models.

## Coverage and Method

- Statically enumerated 194 `capability_action` declarations and 25 dictionary-style tool declaration sites in `src/pal` (including dynamic expansion); this is not the number of tools mounted online.
- Inspected 118 generated top-level input models and verified schema defaults and validation behavior.
- Cross-checked Core/Worker resident prompts, skill manuals, direct/indirect routing, alias search, parameter validation, result budgets, snapshots, error recovery, plugin/channel lifecycle, and package/shell completion notifications.
- Covered relevant paths for execution, artifact, browser/search, memory, skill, checklist, proactive, channel, plugins/packages, LLM, LSP, MCP, and Bunshin; inspected the in-repo provider's external health documentation, and the adjacent `pal-shell-native` routing and completion notification implementation. No changes were made to the adjacent repository.
- Isolated reproduction: snapshot coverage, nullable defaults, skill patches, error result construction, MCP error presentation, inventory size.
- Ran relevant existing regression tests: 53 passed, 2 depending on deprecation warnings. The full test suite was not run; no real browser sites, remote MCP services, or online model decisions were tested. Therefore the “detour” descriptions below are path risks arising from visible contracts, not claims of measured production incidence rates.

Priorities: P1 = correct calls may still lose evidence, report false success, or enter error recovery; P2 = clear extra call/retrieval/context cost; P3 = lightweight optimizations that can be implemented alongside merges.

## E01 · P1 · Arbitrary Attachment Snapshot Mistaken for Complete Tool Output

Location: `src/pal/execution/runtime.py:1294`; `src/pal/web_fetch/capabilities.py:415`.

When the budget layer finds `snapshot_refs` non-empty, it directly reuses `refs[0]` and outputs “Complete output snapshot”. But the reference may only be a component such as the body or stdout, not the entire `llm_text`.

Isolated reproduction: browser_navigate returned a body `body` and 80 long links, with output limited to 1,000 characters. The result was success with a single snapshot, and the file content was exactly `body`. The links were truncated in the preview, but the “complete output” notice still pointed to this 4-character file. A subsequent read_file cannot recover the links.

Remediation: distinguish attachment references from complete-result references; budget truncation must save the complete visible result, or provide an explicit manifest covering all omitted fields. The presence of “a file” must not be used to infer that “the file contains the complete result”. Preserve the current reference lifecycle and immutability semantics.

Acceptance: very short body with long metadata/links; multiple attachments; failed results with snapshots; nested call_tool — in all cases the omitted content must be recoverable via the returned file without re-execution.

## E02 · P1 · Defaults Inconsistent with Input Schema

Location: `src/pal/execution/generated_tool_models.py`, e.g. `:19`, `:40`, `:130`, `:635`.

Among the 118 generated input models, 164 top-level fields have defaults that violate their own field schema, typically `(str, Field(None))` / `(int, Field(None))`: omission is allowed, yet the schema publishes a `default: null` that the type does not accept.

Reproduction: sending an attachment `{path: '/tmp/file.txt'}` passes validation; adding `caption: null` is rejected. The same contradiction pervades the artifact, memory, core, and other tools. If a model follows the defaults and fills in null, it wastes a failed call; read_tool still presents the same contradiction.

Remediation: unify field semantics. Fields that genuinely allow null should be declared nullable; fields that only allow omission must not publish an invalid null default. Verify whether omit/null/empty string have distinct business effects; do not indiscriminately treat null as “clearing”.

Acceptance: iterate over all schemas and `$defs` checking defaults; verify that the four input kinds — omitted/null/empty/valid — match the actual execution semantics.

## E03 · P1 · skill_update Silently Ignores the Patch but Reports Success

Location: `src/pal/execution/generated_tool_models.py:1156`; `src/pal/skill/service.py:91`.

`patch` has only an arbitrary-dictionary schema with no field contract. The implementation generally uses `new_value or current_value` for strings and lists, does not reject unknown keys, and unconditionally bumps the version.

Isolated reproduction:

- `activation_terms: []` and `avoid_when: ''` cannot clear the old values.
- `manual: 'new text'` (the real field is manual_text) is silently ignored.
- All of the above operations return an updated object, with the version going from 1 to 2.

The result is that Pal either falsely declares success, or re-reads, guesses fields, and retries. read_tool also cannot tell it the exact internal fields of the patch.

Remediation: use an explicit patch model and reject unknown keys; decide modifications by field presence and handle empty values per business constraints; no actual change should return a no-op without manufacturing a version bump. Explicitly distinguish non-clearable fields from clearable fields.

Acceptance: legitimate modifications, misspelled fields, clearing lists/text, empty patches, identical-content patches, and uncertain retries after a successful apply.

## E04 · P1 · Error Causes Fail Again or Get Hidden on the Way Back

Location: `src/pal/memory/capabilities.py:139,147,157,177`; `src/pal/plugins/l3/sqlite_vec.py:450`; `src/pal/mcp/plugin.py:270`.

A whole-repo AST scan found 5 `IntrospectionResult` constructions missing the required `llm_text`. Affected paths include missing/misconfigured dreaming service, unavailable archive, and memory maintenance refusing detach.

Reproduced: when the dreaming service is missing, the response should be unavailable but instead throws `TypeError: ... missing ... llm_text`. The runtime can catch the exception, but what the model receives is a wrapped error and it cannot correct its action based on the original business cause.

Another reproduction: the concrete error of mcp_image_prepare is `image file not found: /tmp/missing.png`, but the model-facing text is only `image prepare failed: ValueError`; the concrete message is kept in structured, and failure rendering does not automatically expand these details to the model.

Remediation: unify error result construction and safe projection, preserving sufficiently concrete business causes and the shortest recovery action; do not dump arbitrary internal exception objects or sensitive data wholesale to the model.

Acceptance: service not mounted, under maintenance, no archive, parameter errors, missing file; the model-visible text must contain an actionable concrete cause, and no secondary construction error may occur.

## E05 · P1 · Memory Recovery Prompt Requires Exact Query, but the Public Entry Only Offers Similarity Search

Location: `src/pal/memory/capabilities.py:496,557`; `src/pal/plugins/l3/sqlite_vec.py:544`; MemoryQuery in `src/pal/memory/contracts.py`; `src/pal/memory/schema.py:48`; `src/pal/memory/repository.py:608,656`.

Recovery from uncertain update/delete results requires “recall that mem_ref”. But recall_memory only offers retrieval parameters such as queries/topic/kind, with no exact mem_ref parameter. The implementation uses FTS, LIKE, and vector fusion; FTS marks document_id as UNINDEXED, and LIKE does not match IDs either.

Therefore, putting `fact:...` into queries is not a reliable by-ID read of the current record. memory_history can read history by ref, but its contract explicitly states it is not the current fact and cannot substitute for reconciling current state.

Remediation: provide an exact query path for current records (recall_memory may be extended), returning present/deleted/superseded/not_found plus any available successor; recovery prompts must carry the exact parameters. A search miss must not serve as evidence that a deletion completed.

Acceptance: update succeeded but response lost, delete succeeded but response lost, a ref superseded by dreaming, and records whose content does not contain the ref at all; the status must be determinable without semantic search guessing.

## E06 · P1 · PDF Page Index Already Exists, but Page-Level Visual Reading Is Still Not Closed-Loop

Location: `src/pal/artifact/service.py:671,933`; `src/pal/artifact/capabilities.py:84,218`; `src/pal/execution/generated_tool_models.py:75`.

Auto-attached images always take the first image representation; artifact_select only accepts an artifact_id and refreshes the TTL, and cannot select a page or representation. read_artifact/read_file are both text reads.

When the user asks to "see the image on page 2", the model may know the image_file_path, but there is no direct attachment entry in the existing PDF representation. A possible workaround is to artifact_import that PNG again, creating a new artifact; but import's do_not_use_when also says not to use it when an artifact_id already exists. Pages beyond the pre-rendered range also have no on-demand visual path under the same contract.

Confirmed remediation: at parse time, generate per-page body text, full-page rendered images, and an index of pages 1…N, while keeping rg-friendly flat text. When visual content is needed, use the existing artifact_import to import the image_file_path from the index; revise the import guidance to make clear that the PDF parent artifact_id does not mean page images have been injected. No page-selection tool will be added.

Acceptance: page 2 of an image-based multi-page PDF, pages beyond the pre-rendered range, mixed text/image PDFs, non-vision models, exhausted image budget; "path was returned" must not be treated as "pixels were seen".

## E07 · P2 · The browser still has other places where information is lost at collection time

Location: `src/pal/web_fetch/browser_service.py:625,1046,1071,1140`.

The already-fixed body/snapshot/find/evaluate do not mean all browser output is complete:

| Data | Current behavior | Recoverability |
| --- | --- | --- |
| links | The DOM script only takes maxLinks+1, and the capability then trims to max_links (capped at 500) | text_file contains only the body; hrefs beyond the budget require re-extraction |
| inspect_layout | Only takes the first max_elements, at most 20 | Has total count/truncated but no offset/cursor; requires changing the selector or doing your own evaluate |
| click's open_tabs | Strings are trimmed to the first 12,000 characters | The hint says it can be obtained via browser_tabs list; a recovery route exists, but it adds one more collection pass |
| network request body | Trimmed to 2,048 characters at collection time | Per-body truncation/original length is not marked; network pagination can only page through entries and cannot recover the tail of a body |

Remediation: establish a unified budget table by data type. Text/structured output uses complete snapshots or resumable cursors; where lossy network sampling is truly needed, explicitly declare the sampling scope, truncation, and recoverability, so that requests are not resent just to gather supplementary evidence. This does not mean removing all caps.

Acceptance: 501+ links, 21+ same-type elements, long tabs output, long request bodies; anything recoverable via pagination or files must be recoverable, and anything not recoverable must be honestly stated.

## E08 · P2 · Some actions' conditional parameters and real boundaries are not in the schema

Location: `src/pal/web_fetch/tool_models.py:48,155` and BrowserExtensionManageInput; `src/pal/web_fetch/browser_service.py:655,882`.

- browser_extension_manage only enumerates operation; both path/extension_id can be omitted with no conditional description. mount actually requires path, and reload/unmount actually require extension_id.
- browser_find's text/regex mutual exclusion currently relies on prose and runtime checks; the schema does not require either.
- Some numeric boundaries of resize, scroll, and layout are only clamped in the implementation; for example, resize.width=100 passes model validation but actually executes as 320. The model has to infer limits from return values.

Remediation: write the known required relationships, mutually exclusive relationships, ranges, and units into the input models/field descriptions; make the first call succeed whenever possible. Use explicit input rejection for business conditions; don't let read_tool return a schema that still fails to explain the problem. Keep reasonable defaults.

Acceptance: minimal valid input per operation, missing conditional parameters, mutually exclusive inputs, range boundaries; silently executing action parameters different from the request without explanation is not acceptable.

## E09 · P2 · Some navigation and capability descriptions still do not match real capabilities

Location: `src/pal/identity/capabilities.py:52`; `src/pal/memory/capabilities.py:134`; `src/pal/core/capabilities.py:68`; `src/pal/execution/tool_registry.py:824`.

- identity_show recommends behavior_show, which has no public alias in the repo.
- memory_dreaming recommends `update`, whose actual public name is update_memory.
- core_configure gives normal/maintenance as examples, but the implementation merely assigns a string to state.mode. The repo's maintenance/drain control is a separate state mechanism; this does not guarantee stopping task intake, draining, maintenance, or recovery.
- The existing reference projection mainly recognizes canonical paths, and next_tool_hints does parsing; wrong public aliases in ordinary prose are not uniformly detected.

Remediation: fix these specific references; clarify core_configure's real effect, and do not pass off a state label as a lifecycle capability. Add public alias reference checking for built-in guidance/skills, and distinguish conditionally available plugins from nonexistent tools; do not crudely ban all optional plugin references.

Acceptance: with optional plugins unmounted, after Worker trimming, and after alias renames, hints still point to executable paths or honestly state the absence.

## E10 · P2 · Discovery information still has one full-inflation path and one systematic duplication

Location: `src/pal/execution/runtime.py:398`; `src/pal/execution/tool_presentation.py:49`; `src/pal/mcp/compiler.py:351`.

- exec_tools returns the description + search_text + input_schema of the entire inventory. search_text is index material and duplicates the description; tools have no filtered/condensed view. Loading only the 11 execution tools in isolation, the rendering already reaches **17,424 characters**. That is a character count, not a token count; no assumption was made about the number mounted in production.
- MCP's `_mcp_guidance` puts the external full description into both purpose and use_when, so the rendering and search contracts duplicate the same content. The longer the external tool description, the more obvious the cost.

Remediation: the inventory by default provides a short searchable catalog, with the full contract still obtained via search/read; filter out internal index fields. MCP keeps one complete description, and use_when supplements the applicable conditions instead of copying. Only deduplicate and layer; do not cut information needed for invocation.

Acceptance: with a multi-tool inventory and long MCP documents, compare character/token volume; the first-time success rate of correctly choosing a tool and parameters must not regress.

## E11 · P3 · Capability-based model selection still requires list followed by show one by one

Location: LLMModelListItem and list_endpoints in `src/pal/llm/capabilities.py:118`.

llm_list only provides name/provider/wire_shape/priority and the like, not vision/tools/context_window. When the user says "switch to a vision-capable model", a Pal with unknown configuration must first list, then llm_show each one, in order to choose. The calls can be parallel, but the extra round trips remain.

Remediation: add a minimal selection summary or capability filter to the list, without returning the entire capabilities_blob. Keep llm_show for the detailed contract.

Acceptance: when only one of multiple endpoints supports vision, the list result alone is sufficient to make the choice; auth information must not be leaked.

## Checked but not recommended for rework this round

- alias tokenization by underscores, English domain/action/object, exact alias first, 1–3 results by default, purpose as a synonym supplement: keep. use_when/do_not_use_when were not wrongly included in positive scoring.
- The distinction between direct/indirect and the Worker's actual exposed surface: keep. Shell-native Workers have actual indirect session/status capabilities, so read/call cannot be removed across the board.
- file read/edit's digest/CAS, ranged reads, and independent-call batching: keep; the protection protocol must not be bypassed to save round trips.
- package and native shell completion notifications: keep waking via the harness, not reverting to model polling; related regressions this round passed.
- The responsibilities of plugin attach/reattach and channel reload/restart, proactive's current channel and the single default destination: keep the existing division of labor.
- checklist auto-close on completion, skill body injection via context_messages, and evidence reuse for persistent prompts: these already-fixed paths were not relisted as new defects.
- One initial suspicion about browser error codes was ruled out: the real sidecar converts worker ValueError into invalid_arguments, so the missing_execution_scope result obtained by bypassing the transport with a direct mock cannot be used to assert that the production path is wrong.
- MCP's conservative execution semantics of not trusting remote readOnlyHint, Bunshin approval and role boundaries, and offline host restart boundaries are permission/correctness requirements, not efficiency costs that can be freely deleted.
- Audio transcription compute will not be expanded for now, per the user's established decision.

## One-Time Remediation Organization

It is recommended to implement this as three mutually coordinated change groups, with an overall review completed in the same round, rather than re-asking about remaining issues after fixing each tool:

1. **Contracts and recovery**: E02/E03/E04/E05/E08. Unify input defaults, conditional fields, patch, exact reconciliation, and error envelope.
2. **Output and reading**: E01/E06/E07. Unify full output/component references, enable PDF visual page selection, and clarify recovery strategies for all browser truncation.
3. **Discovery and prompting**: E09/E10/E11. Fix incorrect references and false effect descriptions; layered cataloging, deduplication of repeated information, minimal selection summaries.

Final acceptance is based on coverage of user task paths, not just string assertions:

- Discovery → first effective call; validation failure → one clear correction.
- Action succeeded but response lost → exact reconciliation, without redoing side effects.
- Large results → recoverable file/cursor, without re-collecting.
- Multi-page PDF → directly select pixels of the needed pages; degrade honestly when vision is unavailable.
- Query/modify skill → unknown fields rejected, clearing is valid, no-op does not create a version.
- Verify routing, file reachability, and snapshot lifecycle separately for the Bunshin/worker and the main Pal.

Keep the existing full CI batching strategy. If model benchmarks are run later, they should be extended to the above end-to-end paths, comparing first effective call, completion rounds, duplicate side effects, output recoverability, and token cost; search top-1 alone cannot prove the whole path is more efficient.

## Verification Commands for This Round

```sh
python -m pytest -q \
  tests/test_tool_search.py \
  tests/test_tool_discovery_contracts.py \
  tests/test_worker_tool_routing.py \
  tests/test_result_guidance_budget.py \
  tests/test_package_completion_delivery.py \
  tests/test_proactive_destinations.py
```

Result: 53 passed, 2 warnings. Static scanning and isolated reproduction did not modify the running Pal or business configuration.

## Confirmed Remediations and Acceptance Paths

- E01: Snapshots add coverage descriptions; the budget layer uses content digests to decide reusability, and body or stdout fragments must not be treated as complete results. Old references default to unknown; keep L1 ownership and file read/write permissions.
- E02/E08: omit-only parameters no longer publish invalid null defaults; nullable parameters retain empty-value semantics. Memory public queries default to limit=5, view=summary; browser conditional parameters and ranges enter the schema and actual validation.
- E03: skill patch uses an explicit model and rejects unknown keys; auxiliary text/lists support clearing, name and body must be non-empty; no-op does not write files or increase the version.
- E04: Fixed error results missing llm_text; MCP raw reasons enter visible text, with diagnostic routes given by parameter, file, and service connection.
- E05: recall_memory's mem_ref exact queries go directly to persistent records, without being blocked by a pending index; keep the two phases of persistence and indexing. Replacement and deletion states are returned based on persistent evidence, without forcing verification after every write.
- E06: Per-page text and full-page images; PDF does not inject the first page by default; import on demand, keeping vision and budget controls. Page-count limits/per-page processing failures are surfaced as partial.
- E07: Fully collected text such as links/layout/tabs is persisted to disk; the request body remains an explicitly labeled sample, and requests are not re-sent to gather evidence.
- E09/E10/E11: Correct incorrect alias and core mode declarations; inventory shows a brief catalog with full contracts retained; MCP descriptions deduplicated; model list provides a capability selection summary.

Locally only relevant regressions were run; full tests are executed by GitHub CI after push. No model benchmark was run, so offline test passes cannot be used to claim that the online first-call success rate has improved.

Remediation acceptance: 20 relevant test files with **286 passed, 17 subtests passed**, and only two pre-existing dependency deprecation warnings. Coverage includes parameter contracts, discovery routing, skill, exact memory, MCP, snapshots and budget, browser scripting, Bunshin/worker, PDF and image injection, package completion notifications, and LLM usage. Full CI is left to GitHub; nothing was deployed to running instances.

The final schema review also added the index condition for browser_tabs select and existing string length boundaries; after that, all 85 relevant targeted regressions passed. The final MCP error classification review covered missing files with artifact in the name, avoiding misleading routing to artifact queries.

## Follow-Up Scan Remediations

- Snapshot reads reuse the original reference and do not depend on the removed source-file edit manifest; prompts suggest locating with `rg -n` / `wc -lc` on demand before reading a range, with overlong lines excerpted from the original file.
- proactive schedule rejects unknown fields, type errors, scheduled parameters missing cadence, and mutually exclusive fields, clearly returning a task parameter error; legitimate manual defaults are kept and outdated date examples removed.
- Memory conflicts directly show the existing reason / successors; degraded recall exposes its reason and does not claim that unreturned results are definitely nonexistent.
- MCP keeps the full protocol result internally; the presentation layer removes extra copies of tool bodies, error bodies, and prompt messages, while keeping service diagnostic hints.
- CI fixes the snapshot recursive persistence regression, outdated contract assertions, and macOS temp directory symlink comparison.

Final local relevant regressions: **114 passed**, with warnings from pre-existing dependency deprecation notices. PDF parsing performance, MCP image routing, and source-file scanning policy remain unchanged per discussion; full verification is left to GitHub CI, and no running instances were deployed.

## Additional Remediations for Recovery Hints and Status Visibility

- Skill re-enable hints explicitly state that both status=active / enabled=true must be set; reads and searches show both enabled and actual active, preserving the semantics of each field.
- Shell model-visible results show the exit code and stdout/stderr labels, retaining timeout, cancellation, and persistence failure descriptions.
- Checklist creation rejects duplicate steps after whitespace trimming and keeps the original plan; step-match failures directly provide the current plan, and oversized results are recovered via the existing snapshot mechanism.
- Proactive run history clearly distinguishes unavailable, empty, and available; absence of a history repository no longer implies it never ran.

Local relevant regressions: 126 passed, 17 subtests passed, two pre-existing dependency deprecation warnings. Full testing is left to GitHub CI; no running instance was activated.

## Output Contract Errors and LSP Diagnostic Evidence

- Output schema validation failures keep the specific error, original body, and unvalidated output; errors are clearly attributed to the tool/provider contract and do not steer toward modifying task parameters. Large results continue using budget-layer persistence, and error outputs that cannot be JSON-encoded can still be delivered.
- When LSP diagnostics have not arrived, the outer layer marks unavailable / pending and provides routes for retrying after waiting and for diagnosing persistent failures, without claiming fresh.
- Diagnostic notifications check the document version, filtering out version-mismatched notifications without waking waiters; notifications without a version are kept as version_unknown, and the outer layer does not claim a match with the current file. Document versions are registered before sending notifications to avoid misjudging immediate responses.

Relevant regressions: 86 passed, two pre-existing dependency deprecation warnings. Full verification is executed by GitHub CI; no running instance was activated.

## LSP Outer-Layer State Isolation

- RPC/startup errors return only the current operation and current error, no longer expanding historical results; historical observations are clearly isolated in lsp_show's cached_snapshot.
- manager_running / manager_owned are determined by the process state from this run, and liveness is not inferred from a cached ok; the current last_error is not overwritten by historical fields.
- lsp_rescan preserves the scan status and configuration errors returned by the manager; old errors are cleared after a successful recovery. The recovery notes in show distinguish configuration fixes from lifecycle recovery.

Related local regressions: 43 passed, with two existing deprecation warnings. Full verification is delegated to GitHub CI; the running instance was not activated.

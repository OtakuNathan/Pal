# Tool token efficiency

Tool descriptions retain purpose, applicability, examples, special next-tool
conditions and effect/retry guidance. Shared routing instructions appear once
in the developer prompt. Search hits carry a compact input contract as well as
the use/avoid conditions, so a valid first call need not read the full tool
definition. Output schemas remain in
registry records and structured introspection responses for validation and
external callers; they are omitted from LLM descriptions and discovery text.

Only Pal-owned JSON framing is compacted. String results, including source,
CRLF, tabs and trailing whitespace, pass through unchanged. Pagination budgets
apply to the rendered body and pages concatenate back to that body.
`search_tools(module_name=...)` applies the same module filter as introspection.
Word matching and alias splitting are isolated to execution discovery; they do
not change memory or skill retrieval.

Plugin lifecycle names have separate responsibilities: `plugin_attach` loads an
enabled detached plugin and preserves an attached instance; `plugin_detach`
unloads it; `plugin_reattach` unloads and reloads it in one call, coordinating
affected dependents. Existing integrations that used attach as a reload must use
reattach. Package install/prepare discovery covers both plugins and channel
providers.

For Bunshin, reuse known skill contracts, discovery results, and approval for the
current workflow. `task_spec_file` accepts an absolute local UTF-8 requirement path;
the Manager snapshots its exact text, source path, byte count, and digest into the
immutable task ledger. This does not add the source directory to the worker's
workspace. Conversation requirements still use `task_spec.authoritative_text`.
Additional structured task fields can accompany the file, but its authoritative
text and source metadata belong to the harness.

## Paired evaluation

`benchmarks/tools/efficiency-v2.json` defines 13 cases, three repetitions per
variant, five rounds maximum and an 8192 output-token budget. The standalone
runner uses real discovery/contracts and the configured endpoint, with isolated
file fixtures and simulated shell/LSP effects. It does not open a resident Pal
runtime, run shell commands, or change the online database/configuration.

Run from a checkout containing the runner:

```sh
PYTHONPATH=src python -m pal.tool_efficiency_benchmark \
  --runtime-root /path/to/runtime \
  --base /path/to/base-worktree --search /path/to/search-worktree \
  --output /path/to/new-private-report-directory
```

The runner snapshots one enabled endpoint, locks the model and disallows
fallback. It alternates variant order and measures provider-reported input plus
output tokens; reasoning tokens are reported separately, never counted twice.
Discovery totals use fixed case categories. Reports contain fixture transcripts
and endpoint metadata and must stay outside the repository.

The search projection is selected only when both variants pass quality gates,
completion/argument/recovery/selection rates do not regress, median request
rounds do not increase, discovery tokens decrease and total tokens do not
increase. Missing usage, incomplete pairs or unsafe operations prevent selection.
Otherwise retain the base only if its own validation passes. Neither variant
passing is an unresolved evaluation, not approval to deploy.

These changes include resident Core/Execution code. Deployment requires a
separate full restart; source checkout or plugin attachment does not activate it.
The current tool-surface remediation adds local regressions for selection,
callable search contracts, browser navigation, screenshot projection, and
proactive destination binding. Run the paired model evaluation only when API
quota is available; local tests do not measure model token usage.
`benchmarks/tools/affordance-v1.json` adds four task prompts with isolated
browser and proactive fixtures. Pass `--manifest benchmarks/tools/affordance-v1.json`
to the standalone runner once both compared revisions contain the new fixture
support. This is a behavioral diagnostic: an older baseline can fail an
affordance case, in which case inspect the per-revision reports rather than the
comparison's token-efficiency selection. The v2 manifest remains usable against
an older baseline checkout.

Package installation, dependency preparation, and uninstall briefly wait for a
terminal result (`wait_ms`, default 1000, maximum 5000). Longer jobs report
notification availability. When a notice is scheduled, Core delivers a runtime observation to Pal on success or failure and starts a
continuation when idle. The job does not directly send a message to the user;
Pal decides how to continue the task and report its outcome.
The result includes actual activation state; notification failure does not change
the package outcome. Notices are best effort within the live runtime, not a
durable delivery queue across restarts. Use `package_status(job_id=...,
wait_ms=...)` when notification is unavailable or diagnostics are needed.
The resident job handlers do not hold a lifecycle read fence while waiting;
the package worker owns the activation write fence.

Schema-generated examples remain internal validation fixtures. Only explicitly
authored examples appear in tool descriptions, so placeholder paths, commands,
and no-op edits cannot be mistaken for recommended invocations.

Text artifacts expose a managed `text_file.file_path` in their reference and
prompt manifest. Search it with `run_shell` and `rg`, then use `read_file` for
line ranges; handle oversized lines with shell tools. Artifact previews remain
bounded, but the supplied path contains the complete extracted text. Short
inline text is the actual complete content, not a shortened preview. PDF text
includes original page numbers and line ranges, including gaps for blank pages.
Managed paths are temporary read-only inputs. Audio uses the same path when a
transcript exists; no transcription backend is added by this change.

Artifact search merges identical snippets while retaining every representation
and selector in `locations`. LSP text projections omit an identical nested
`evidence.result`; the structured result and evidence metadata remain intact.
`call_tool` accepts the model's chosen arguments without a preliminary schema
read. Argument validation errors direct it to `read_tool` for the exact schema.

## Alias-first discovery

Search prioritizes exact aliases, then their unordered underscore-separated
words, then alias subsets/prefixes. Purpose supplies additional task words and
synonyms. Applicability prose and registry classifications do not supply positive
query evidence; classification filters still work. Prefer English queries in
`[domain] [action] [object]` form when the domain is known. Spaces and underscores
both work; not every alias has all three components.

Default results contain at most three strongest matches, with exact aliases or
matching word sets converging to one tool when unambiguous. An explicit `top_k`
or `limit` enables broader discovery. Hits retain the callable input contract.

`scripts/benchmark_alias_search.py` evaluates model-generated first searches and
first selections against isolated real registries (112 tools in this fixture).
It performs schema validation only, never executes business tools, and uses the
configured GLM 5.3 endpoint without model fallback. The two-round protocol allows
one search and one selection, with no repair calls: it does not measure eventual
business success. Cases cover memory, LSP, plugin/package lifecycle, browser,
artifact, and file tools. Missing searches count as failures. Use a source copy
of the current working tree before modification for `--base`, not an unrelated
Git HEAD that omits other pending changes. Outputs contain fixture responses and
must be stored outside the repository.

```sh
python scripts/benchmark_alias_search.py --runtime-root /path/to/runtime \
  --endpoint glm-5.3 --base /path/to/before --candidate /path/to/after \
  --output /path/to/new-private-report-directory --repetitions 3
```

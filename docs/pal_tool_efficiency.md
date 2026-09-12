# Tool token efficiency

Tool descriptions retain purpose, applicability, examples, input contracts,
next-tool routing and effect/failure/retry guidance. Output schemas remain in
registry records and structured introspection responses for validation and
external callers; they are omitted from LLM descriptions and discovery text.

Only Pal-owned JSON framing is compacted. String results, including source,
CRLF, tabs and trailing whitespace, pass through unchanged. Pagination budgets
apply to the rendered body and pages concatenate back to that body.
`search_tools(module_name=...)` applies the same module filter as introspection.

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

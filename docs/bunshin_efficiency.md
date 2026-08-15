# Bunshin Efficiency Telemetry

Operator reference for the `pal bunshin efficiency` command.

## Usage

```
pal bunshin efficiency WORKFLOW_ID [--json] --runtime-root RUNTIME_ROOT
```

The command reads the Bunshin v2 workflow database **strictly read-only**
(it opens the SQLite file in `mode=ro`, never creates or migrates it) and
prints an efficiency report for one workflow.

- `WORKFLOW_ID` is the workflow identifier to report on.
- `--runtime-root` is required and selects the runtime root holding the
  Bunshin v2 storage, exactly like every other `pal` subcommand. The
  database file is always resolved by `bunshin_db_path(runtime_root)` to
  `RUNTIME_ROOT/data/bunshin/bunshin.sqlite3` — no environment variable or
  other flag can redirect this read path.
- `--json` selects the machine-readable document instead of the default
  text report.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | A report was printed, even if some metrics were unavailable. |
| 1 | The workflow id is unknown, storage could not be opened read-only, or a telemetry row was malformed. A diagnostic goes to stderr; nothing is printed to stdout. |

## Metrics

| Metric | Meaning |
| ------ | ------- |
| `tool_batches` | LLM rounds that issued at least one tool call. |
| `singleton_ratio` | Tool batches containing exactly one tool call divided by all tool batches, in `[0.0, 1.0]`. |
| `longest_singleton_streak` | Longest run of consecutive LLM rounds whose batch held exactly one tool call. |
| `llm_rounds` | Total completed LLM rounds recorded for the workflow. |
| `token_splits` | Provider-reported input/output/cache token totals. |
| `latency_totals` | LLM, tool, and wall latency totals in milliseconds. |
| `per_role` | The same totals aggregated per role, ordered by role name. |

## Unavailable metrics

Telemetry recorded by older Bunshin versions may not include every metric
(for example provider cache token splits). Such metrics are reported
**honestly as unavailable, never as `0`**:

- In the text report each unavailable metric appears on an explicit
  "unavailable" line with the reason.
- In the JSON document an unavailable metric is `null` with a sibling
  reason field, nested inside the section object that owns it, for
  example:

  ```json
  {
    "token_splits": {
      "cache_read_tokens": null,
      "cache_read_tokens_reason": "not recorded by this storage version"
    }
  }
  ```

Unavailable metrics do not change the exit code; the command still exits
`0` because the query itself succeeded.

## JSON output shape

`--json` prints exactly one JSON object on stdout, deterministic for equal
inputs, with this shape:

```json
{
  "workflow_id": "wf-1",
  "tool_batches": 12,
  "singleton_ratio": 0.25,
  "longest_singleton_streak": 3,
  "llm_rounds": 40,
  "token_splits": {
    "input_tokens": 100000,
    "output_tokens": 8000,
    "cache_read_tokens": null,
    "cache_read_tokens_reason": "not recorded by this storage version"
  },
  "latency_totals": {
    "llm_latency_ms": 12000,
    "tool_latency_ms": 45000,
    "wall_latency_ms": 60000
  },
  "per_role": [
    {
      "role": "manager",
      "invocations": 4,
      "llm_rounds": 30,
      "input_tokens": 60000,
      "output_tokens": 5000,
      "cache_read_tokens": null,
      "cache_read_tokens_reason": "not recorded by this storage version",
      "cache_write_tokens": null,
      "cache_write_tokens_reason": "not recorded by this storage version",
      "llm_latency_ms": 9000,
      "tool_latency_ms": 30000,
      "wall_latency_ms": 40000
    }
  ],
  "unavailable_metrics": ["token_splits.cache_read_tokens"]
}
```

Measured metrics appear as plain numbers; unavailable ones appear as
`null` with a sibling `<name>_reason` field. Token and latency metrics
live under the `token_splits` and `latency_totals` objects, per-role rows
under `per_role`, and the names of all unavailable metrics under
`unavailable_metrics`.

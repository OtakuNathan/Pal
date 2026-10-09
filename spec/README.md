# Formal checks

The canonical executable inventory is `tlc-suite.json`. Every `.cfg` and `.tla`
under `spec/` and `docs/llm_projection_refactor/formal/` must be classified there.
Adding a model without a checked configuration, omitting a config, or leaving a
generated implementation relation stale fails the inventory check in ordinary CI.
That check does not claim TLC ran.

`.github/workflows/tlc-nightly.yml` runs daily at **02:00 Europe/London** and also
supports manual dispatch. GitHub's timezone-aware schedule follows UK daylight
saving time. Scheduled runs use the default branch, so the workflow starts only
after the change reaches that branch. Scheduling can be delayed by GitHub.
See [GitHub schedule documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onschedule).

## What the nightly checks mean

- **positive**: TLC must exit 0 with the model-checking completion message,
  a finished run and a nonempty completed state graph. TLC's successful result
  for an unsatisfiable `Init` is rejected as vacuous.
- **negative**: TLC must exit 12 with exactly the named invariant violation.
  Parser failures, other invariant failures, deadlocks, crashes and timeouts fail
  the job. A model accidentally made vacuous by removing the bug also fails.
- **witness**: an intentionally negated reachability assertion must be violated.
  This establishes a reachable scenario, not a product defect.
- **retired**: not run, explicitly reported with the retirement reason. The three
  historical cache policies are not counted as checked current models.
- **support modules**: generated channel/FD relations imported by checked models.
  The Bunshin implementation topology also has its own checked configuration.

The runner compares all three generated relations to production renderers.
Nine matrix jobs cover seven families, with Bunshin divided into three shards.
A separate conformance job runs production boundary regressions, runner verdict
tests, and the imported-plan tests that generate extra mutant configurations.
This combination can detect known implementation/model disagreements; it is not
an automatic proof that hand-written TLA actions equal every production path.

## Pinned tool and evidence

The manifest pins the official **v1.7.4** jar, TLC 2.19, to SHA-256
`936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88`.
Both downloads and existing local jars must match. The official `v1.8.0` URL is
[a rolling pre-release](https://github.com/tlaplus/tlaplus/blob/master/README.md),
so a version-looking URL alone is insufficient to pin its bytes.

CI uses Java 21, Python 3.12, two TLC workers and a 2 GiB heap per model. Each model
has an 1800-second deadline; a timeout is incomplete validation and fails CI.
Full config files preserve bounds and fairness assumptions in the artifacts.
Per-case artifacts include input TLA/config files and their hashes, command,
exit code, elapsed time and full TLC output (including state counts and traces).
The summary records revision/dirty state, Java version, runner/manifest hashes
and explicit retired cases. Artifacts are uploaded on failure as well as success
and retained for 14 days. Interrupted runs retain incomplete status rather than
being reported as green.

## Local use

```sh
python scripts/run_tlc.py --inventory-only
python scripts/run_tlc.py --fetch-only --jar /tmp/pal-tlc/tla2tools.jar
python scripts/run_tlc.py --jar /tmp/pal-tlc/tla2tools.jar \
  --group execution --output /tmp/pal-tlc/execution-run-1
# Omit --group to run every active config. Each run requires fresh output.
python scripts/run_tlc.py --jar /tmp/pal-tlc/tla2tools.jar \
  --group bunshin --shard 0 --shards 3 --output /tmp/pal-tlc/bunshin-0
# Recheck one exact configuration with a larger local deadline:
python scripts/run_tlc.py --jar /tmp/pal-tlc/tla2tools.jar \
  --case spec/bunshin/VerifierDraftLifecycle.cfg --timeout 900 \
  --output /tmp/pal-tlc/verifier-draft-rerun
```

Existing `check_*_tla.sh` scripts remain convenience entry points; their scopes
vary. Use the canonical runner for complete positive/negative/witness coverage.
Do not run TLC jobs in a shared metadata directory. The canonical runner gives
each case its own working directory and temporary metadata directory.

## Model/runtime boundary review, 2026-10-09

The three generated relations match production code. The endpoint model already
follows selected-endpoint-only retries; item commits versus assembled L1 drafts
are distinct abstractions documented in `llm/README.md`. No fallback behavior was
restored to make an obsolete model pass.

A further cache boundary defect was reproduced: two preparations in one scope
shared `submitted_sequence + 1`, so the second real admission could be ignored.
`PromptCacheTail` stores only one planned payload and could not expose that
interleaving. Runtime admission now uses a once-only identity per plan, with
preparation order separate from admitted count. `PromptCacheAdmission` adds a
two-outstanding-plan model and a shared-sequence mutant; production tests cover
both callback orders and duplicate notifications. This demonstrates a coordinator
boundary defect, not an established main-conversation concurrent-call incident.

Remaining limits: isolated models do not prove cross-subsystem composition,
ordinary callback failure tests do not prove arbitrary external code termination,
and verifier foreground locks do not make external filesystem writes atomic with
durable receipts. See each model's contract map and the original audit report.

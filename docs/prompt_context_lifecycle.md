# Prompt defaults and context lifecycle

Pal's system contract describes identity, interaction mechanisms, capability discovery,
applicability of defaults, evidence, and execution boundaries. The system map describes
possible capabilities; it does not promise that a capability is installed or require
inventory enumeration on every task.

`pal_defaults` blocks in developer guidance apply unless the current user request
specifies otherwise. This is a conditional instruction contract, not an inversion of
provider role precedence. A request not to run tests is honored, and the reply reports
that tests were not run. Actual approval gates, tool protocols and host lifecycle
controls still apply. Bound submission/output contracts are not wrapped as optional
working-method defaults.

Resident and learned behavior remains advisory. Ordinary edit-and-test work does not
require a checklist; use phases for multi-deliverable, long-running or resumable work.
Reuse valid tool contracts and loaded manuals. Maintenance guidance is relevant to
configuration, deployment, activation and unfamiliar runtime constraints, rather than
a compulsory discovery round before each source edit.

For a task that already needs remote execution, prefer configured native targets when
the user has not chosen a connection method. `list_remote` resolves unknown target IDs;
`run_shell(target=...)` executes there. Configuring a remote never changes target 0's
local meaning, and an explicit request for SSH takes precedence over the default route.

## State, events and coverage

The compiler exports context candidates with namespaced source keys. Runtime guidance
and reference blocks are states unless their provider supplies an `event_id`.
Optional `source_revision` must be a positive, monotonically advancing integer for
changed content. Without it the projector advances a revision only when the rendered
semantic value or withdrawal changes. Producers must omit incidental sampling times
and counters from decision context. No new polling is introduced.

- State updates append the complete current block, with key, revision, turn scope and
  replacement/withdrawal action. They do not patch old text.
- Events are delivered once by source key plus event identity. Reusing the same identity
  with different content is rejected. Context loss does not manufacture the event again.
- A valid checklist tool echo can cover the current checklist state, avoiding a second
  synchronous copy of the same snapshot.

`MemoryService.append_l1_prompt_contexts` accepts messages, projection state and the
expected L1 revision. Its single store replacement commits the messages and coverage
together. A stale L1 snapshot is rejected; failed writes cannot advance coverage.
The existing L1 metadata stores `prompt_context_state`; no new database table is used.
Old snapshots without this field initialize on their next prompt build.

Coverage and source revisions are distinct. Missing state may be reprojected under
its existing source revision with a new delivery identity. Unchanged rendering changes
neither the revision nor the L1 record count. Independent one-time tool sidecars retain
their existing stable call-based IDs. On partial-batch interruption, sidecars belonging
to already committed results are included atomically with protocol closure; pending
calls are closed using existing interruption semantics, without invented successes.

## Projection boundaries and authority

Within an unchanged turn projection, committed context positions and the stable
instruction snapshot remain fixed. New states and withdrawals append after the closed
tool batch. The stable snapshot does not capture execution permissions or cancellation:
those remain live execution controls, and a cancelled continuation cannot start its
next call while waiting to notify the model.

Summary/compaction or endpoint/model boundary changes rebuild the stable snapshot and
select the latest effective state. Older control versions and old one-time events are
excluded from that rebuilt view, while raw L1 records remain available. Only genuinely
missing current state is delivered again; its source revision is preserved. New turns
exclude old developer control records, including finalization. User reference material
remains historical user data. Actual operations remain evidenced by their tool results.

Compaction also removes expired developer control records from its transcript input.
A replay anchor containing those records is discarded: normal compaction can build a
fresh request, while a replay-only operation follows its existing unavailable-cache
path. Retaining an old checkpoint must not reactivate expired instructions.

Scope is expressed in sent content as well as metadata. Responses preserves native
developer roles. Existing compatibility codecs carry stable instructions in their
system area and chronological runtime guidance in distinct user content blocks.
These labels aid interpretation; they do not grant developer-equivalent enforcement
or elevate reference documents. Runtime gates are the enforcement boundary.

## Cache and validation

Context synchronization does not decide whether to write a cache checkpoint. Immutable
records can become frontier candidates; the existing incremental economic planner,
profiles and parameters remain unchanged. Legitimate projection changes can invalidate
reuse. Submitted cache markers remain distinct from provider-confirmed reads/writes.

Offline regressions cover 50 unchanged rounds, A/B/A transitions, explicit source
revisions, withdrawal, failure/retry atomicity, context-loss recovery, expired guidance
across turns, three wire shapes, partial-batch interruption and checklist echo coverage.
The real compiler/executor/hook/codec fixture continues to cover 8/50-round cache chains.
No paid LLM or real remote operation is required by these tests.

An isolated 50-round fixture with core rules, memory guidance, one reference and one
runtime state produced the following comparison against `71d7bd2`:

| Measurement | Baseline | Updated |
|---|---:|---:|
| Stable instruction characters (not tokens) | 12,609 | 11,723 |
| Complete prior-message prefix preserved | 0 / 49 | 49 / 49 |
| New L1 synchronization records after first round | 0 (tail rebuilt outside L1) | 0 |
| Initial immutable context records | 0 | 2 |

These are local structural measurements, not evidence of upstream cache reuse or
model round-count improvement. Preserve behavior evaluation cases for: no tests,
explicit SSH, no expression, optional interaction capability discovery, routine edits,
and complex repairs. Evaluate actual behavior and fees through existing usage reports
when real tasks are subsequently run.

## Activation

Resident core/execution changes require an external host restart when idle. Plugin
reattachment alone does not load this implementation. Record the installed commit,
loaded version, offline checks and subsequently observed behavior separately. Preserve
runtime configuration and cache settings when activating or rolling back.

Reproduce the structural comparison with:

```bash
PYTHONPATH=src python scripts/check_prompt_context_stability.py --rounds 50
```

For the baseline, run that same script with `PYTHONPATH` pointing to a clean
`71d7bd2` checkout's `src`. It compares roles and all typed message parts, including
tool calls/results, rather than only visible text.

Behavior evaluation inputs and acceptance criteria:

| User request / fixture | Required behavior |
|---|---|
| “Fix the fixture typo. Do not run tests.” | Make the authorized edit; run no tests; report tests were not run. |
| “Run this on remote A using SSH.” | Use SSH for A; do not replace the explicit connection choice. |
| “Run this on remote A.”, unknown target ID | Discover the configured mapping, then use its native target. |
| “Run pwd here.”, remote A also configured | Execute locally; remote configuration does not redirect it. |
| “Reply in text only; do not change expressions.” | Do not invoke an expression capability. |
| A conversational response could benefit from expression; indirect expression tool available | May discover/use it when appropriate; no mandatory interaction or repeated inventory scan. |
| Routine edit plus focused verification | No compulsory checklist overhead. |
| Several independent deliverables with a resumption requirement | Use meaningful checklist phases and settle the cursor honestly. |

These behavior cases are not automatically declared passed by the structural script.

Local validation used Python 3.13 on Linux ARM. The four CI batches passed with
879 (`core-a`), 875 (`core-b`), 466 (`bunshin-a`) and 478 (`bunshin-b`) tests;
`core-b` skipped seven real-provider tests because their credentials were absent.
Targeted lifecycle, codec, cache and compaction regressions additionally cover the
final changes. Python 3.12 and macOS were not exercised locally. No live runtime was
activated and no paid model request was made.

Before rolling back to software that does not understand scoped context records, use
an appropriate pre-upgrade snapshot or a reviewed historical-reference export; merely
loading new temporary developer records into an older projector would lose the scope
filter. Preserve the raw snapshot for audit.

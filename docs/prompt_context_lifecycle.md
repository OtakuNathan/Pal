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
Reuse valid tool contracts and loaded manuals. For a known plugin, use
`pal.plugin.development` for development, updates and reloads. Use
`pal.self.maintenance` for resident or cross-module changes, runtime configuration
and unclear ownership. Inject a known manual directly when needed.

For a task that already needs remote execution, prefer configured native targets when
the user has not chosen a connection method. Use the `remote` plugin's `list_targets`
if the target ID is unknown; `run_shell(target=...)` executes there. Configuring a remote never changes target 0's
local meaning, and an explicit request for SSH takes precedence over the default route.

`read_file`, `edit_file` and `write_file` are directly exposed file tools. Direct editing
uses the existing read-delivery and stale-snapshot checks. All three accept an explicit
`target`, defaulting to local `0`, just like `run_shell`. Remote integer IDs and exact
configured names/shortcuts are resolved by the remote plugin. Unknown or ambiguous
aliases fail closed; numeric strings are not converted to IDs. File paths are resolved
on the selected worker. A remote file result gains mutation authority only after its
post-budget delivery manifest enters L1 and the worker acknowledges that context
revision. Removing a result revokes its contribution; disconnected workers must
reconcile the latest revision before a dependent mutation.

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

The resident date and checklist are facts, not transient developer controls.
The date is pinned at the end of the stable preamble until a committed compact
or a cross-day new-turn boundary with an elapsed matching cache estimate.
Cross-day activity otherwise appends one UTC date update. Checklist state is
reconstructed from visible accepted tool calls/results and previous recovery
facts; new turns receive a brief count, and lost detail is restored using only
unfinished steps. These Pal-authored chronological records remain visible after
settlement and projection rebuilds until actually compacted, without appearing
as user chat in the dialogue archive. Healthy requests have no runtime reminder.

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

## GPT-6.2 Responses suffix reminders

For verified GPT-6.2 Astra/Sol Responses routes, runtime reminders generated by
the compiler are request-scoped `instructions`, not chronological developer
records. Stable instructions and reference/tool context still use canonical L1
projection. The suffix is included in budget accounting but does not change the
reusable input prefix. Active legacy turns keep their original delivery path;
other models retain the existing chronological context lifecycle.

## Claude turn-scoped system messages

Verified Claude Messages endpoints use `role: system` with
`clear_at: next_user_message` and the `mid-conversation-system-clear-at-2026-08-21`
beta header for round-local harness reminders. The core materializes a typed,
immutable `InstructionScopeIR.NEXT_USER_MESSAGE` record after the closed user/tool
batch and its sidecars, only when preparing an actual model round. Preflight
estimates the record without committing it; retrying the same round reuses the
record verbatim. Once another user turn or tool-result turn follows, Claude
clears its rendered meaning without requiring a history rewrite. These records
remain in the durable L1 and same-route projected prefix, including after turn
settlement. They cannot carry cache markers. Switching to an unsupported model
filters them from that model's projection without deleting canonical history.

Use the native mechanism automatically on documented direct Claude models.
Gateways require verified opt-in (`pal llm add ENDPOINT --replace --instruction-mode
turn_scoped --runtime-root ROOT`). DeepSeek and other Anthropic-compatible
providers retain their existing chronological behavior. If a reminder cannot
legally follow a user/tool-result turn, use the existing chronological fallback
rather than sending an invalid system message. A cleared record is excluded
from local token estimates but remains byte-identical in the API request.

## Gemini interaction-scoped instructions

Native Gemini Interactions can optionally deliver `LLMRequestIR.request_instructions`
through its root `system_instruction`, preserving the standing system text. This
is a semantic interaction scope, not a cache-preserving suffix. Use
`--instruction-mode interaction` to opt in on a native Gemini endpoint;
`auto` (default) keeps the chronological reminder and cache-first policy.
The lineage hash includes the complete root instruction, so changed instructions
are never treated as an identical cached prefix. Incremental encodes include the
root reminder once, never in a frozen history item.

Small isolated provider probes (2026-11-26):

- Gemini 4 Hydrogen: 7,883 input tokens on the cold request; two branches sharing
  the same previous interaction each used 7,937 input tokens. Unchanged root
  instruction read 5,834 cached tokens, changed root instruction read zero.
  Thus the Gemini opt-in remains off in the configured resident endpoints.
- OpenRouter Claude Opus 5.5: first request used 19 uncached + 1,167 write tokens;
  after a new user message, the second used 20 uncached + 1,167 read tokens.
  The temporary BLUE instruction was observed on round one and correctly
  cleared on round two (RED), while its original wire message was retained.
  The configured Opus node uses the verified `turn_scoped` mode.

References: [Claude mid-conversation system messages](https://platform.claude.com/docs/en/build-with-claude/mid-conversation-system-messages),
[Gemini Interactions](https://aistudio.google.com/docs/interactions-overview),
[Gemini caching](https://aistudio.google.com/docs/caching).

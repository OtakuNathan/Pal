# Compact continuity and request preparation

Compact history has one source record in L1. `L1TurnStore` retains its direct
`Continuity` reference; ordinary requests do not search transcripts for it.
Appending/restoring a summary or replacing the working set rebuilds that
reference. Reset and rollback install the reference together with its records.

The first request binds the summary to the first visible, durable user input.
The binding lives in the summary turn's `continuity_anchor` metadata and survives
runtime snapshots. Projection prepends the immutable summary part to that input;
the original user message, attachments and summary record remain unchanged.
If there is no durable user input, an independent user reference is fixed at
the front of history. A request that omits an existing anchor also receives the
reference before history, without changing the saved binding or splitting tools.

Raw summary records and older Pal-authored summary context copies are excluded
from visible history by semantic/source identity. Ordinary user or assistant
text is not deduplicated by content. The summary no longer participates in
turn-scoped context updates, and the template no longer mandates a checklist
query after compaction. Its content remains historical reference material.

Each compact seed has its own message identity. Request boundaries and hot-cache
replay use that identity, so a new compaction invalidates an old replay even if
the summary text is identical. Model/codec changes still use the existing
projection and cache rules. This change does not introduce a general wire cache.

## Compatibility and ownership

`MemoryPackRequest.include_l1_recent_context` defaults to `True`. Legacy callers
keep transcript output; typed requests and replay explicitly disable it.
`MemoryPack.current_summary` retains its existing value semantics, backed by the
source record. The compatibility compiler merges the summary into the first
user message and does not advertise it as a dynamic context candidate.

Native shell now adds at most one `shell_session` capability hint. It lists only
the actions applicable to the delivered state and retains `read_tool` as the
discovery entry point. Already-known operations remain callable directly.
Result status, session ID, errors, effect semantics and output snapshot references remain
available. Pagination hints are independent of the session hint. Native input
schemas, state transitions, cancellation, observation and cleanup are unchanged.

## Validation (Linux, Python 3.13)

- Regression coverage includes 50 unchanged request builds, multiple turns,
  streamed tool rounds, all wire shapes, multimodal input, repeated compaction,
  snapshot restore, rollback, reset, legacy copies and hot-cache replay.
  The final focused continuity/L1/context/hot-cache/Bunshin run passed 82 tests.
- Broader Pal prompt/cache/architecture/runtime-state/behavior checks: 252 passed.
  Prompt infrastructure/cache policy/hot-cache/Bunshin/control-plane checks:
  157 passed. These groups overlap and are not a combined unique test count.
- Native host/production/remote/observation/contracts run: 82 passed initially;
  the old prototype paging test needed adaptation because session IDs no longer
  come from individual action examples. Its rerun, the capability contracts and
  a new production paging/status test passed together (6 tests).
- The runtime-compaction suite still encounters the previously reproduced memory
  review UI assertion: it expects the old batch-review wording, while the
  existing UI presents one candidate at a time. That unrelated feature was not
  changed. The initial memory/L1/compaction group otherwise passed 77 tests.

For 100 frozen turns plus one summary, 50 calls to the previous `build_pack`
performed 10,100 transcript conversions; the typed path performs zero. The
comparison used the previous method retained in `/tmp/pal-native-plugin`, with
the same working set, and checked equal summary and settled-turn results.
Observed times were 0.284 s and 0.006 s; traversal counts are the reproducible
claim, not an end-to-end speedup. Baseline method and measurements are retained
under `~/.local/state/pal/continuity-optimization/`.

No paid provider calls, server-cache validation, activation or release were
performed. Native remains version 0.4.0. macOS and Windows were not tested;
the unchanged shell state machine did not require a new TLA+ model.

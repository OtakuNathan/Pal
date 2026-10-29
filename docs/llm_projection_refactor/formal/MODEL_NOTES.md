# Model Notes and Verification Boundaries

## Files

- `EndpointProjection.tla`:abstract safety specification.
- `single.cfg`: 1 scope, 2 endpoints, 2 tool call IDs, at most 2 attempts/2 history blocks.
- `isolation.cfg`: 2 scopes, 2 endpoints, 1 tool call ID, at most 1 attempt/1 block per scope.
- `stale_replay_mutant.cfg`: deliberately modifies only semantic calls in Repair without touching the native inventory; expected to violate DraftAligned.
- `explore_model.py`: standalone Python finite-state implementation; not TLC, does not validate TLA syntax.

## Already Executed

Python 3 single-scope model exhaustive search: 3,818 states, 8,339 transitions, no listed invariant failures.

Python 3 dual-scope model exhaustive search: 26,244 states, 92,016 transitions, no listed invariant failures.

After deliberately introducing the stale-native repair defect, the Python model found a counterexample within 42 discovered states:

```text
Init
Begin(0, ('a',))
Deliver((0, 0, 0, 'A', 1))
Repair(0)
=> DraftAligned violated
```

This proves that the abstraction assertions of this package can reject the defect; it does not prove that existing/future Pal product implementations are correct, nor that the TLA and Python semantics are fully consistent.

## TLC Verification Update (2026-10-09)

This time, real TLC was executed using the official v1.7.4 (TLC 2.19) with Java 21:
the positive cases `single.cfg` and `isolation.cfg` completed, and `stale_replay_mutant.cfg` exited with exit 12,
triggering the expected `DraftAligned` counterexample. The Python state counts above still belong only to the Python implementation,
and are not TLC state counts. The three configurations have been added to `spec/tlc-suite.json` and nightly CI.
For the tool SHA-256 hashes and how to run them, see `spec/README.md`; for this round of evidence, see
`docs/reviews/state_machine_20261009/nightly-validation.json`.

The original delivery environment could not download the jar, so SANY/TLC had not yet been executed at that time; this limitation has now been closed by the actual checks in this round.

On a machine with Java and a trusted `tla2tools.jar`:

```bash
export TLA2TOOLS_JAR=/absolute/path/to/tla2tools.jar
./run_models.sh
```

The script parses first, then runs the two positive-case configurations, then verifies that the mutant indeed fails with a DraftAligned counterexample. The tool version, jar hash, and Java version used should all be recorded in the release evidence. The script does not access the network automatically and does not install tools on behalf of the user.

## Abstraction-to-Implementation Mapping

| Model | Implementation Obligation |
|---|---|
| slots[s] | Exclusive owner per logical session; must not be merged by endpoint |
| generation | Projection lineage generational change; the implementation must separately maintain history_epoch and projection_generation |
| fence | Authorization of the current worker/writer; late-message checks that do not invalidate older legitimate native data |
| history + native atomic Commit | Same durable acceptance/checkpoint generation; not a cross-store "eventually syncs" |
| cache is a Project prefix | Only consumes trusted append receipts; must not blindly trust even equal lengths |
| Repair updates nativeCalls | ProviderPolicy accepts one legitimate repair result; rewriting signature/encrypted bytes is not allowed |
| StartTool/FinishTool | Existing execution ledger; a started mutation without a result must not be pruned |
| Deliver identity checks | request/session/owner/binding association; error response usage still requires independent settlement |
| Switch drops native and rebuilds | Only possible when the target profile supports semantic import; otherwise an explicit failure |
| Compact clears the model history | Abstract fresh compacted base; the actual summary, retained history, and source CAS are tested separately |

The model's Begin pre-selects the tool set for that output and uses a single Deliver event to abstract the complete provider reply. Actual SSE framing, ITEM_COMMITTED early execution, provider interruptions, and output recovery must be tested separately with combination tests.

## Unproven Properties

- Whether the server-side prompt cache hits, TTL, billing, routing, tokenizer, or hidden templates.
- Full equivalence of HTTP JSON/raw responses; validity of signatures and encrypted content.
- Recovery of external side effects after an open-tool hard crash. Restart only at a closed safe point.
- The effect of live switching configuration on the provider's native inference state.
- Atomicity of real persistence, identity authentication, process death, permission isolation.
- Liveness: no fairness assumptions, no promise that the provider/tool always returns; `CHECK_DEADLOCK FALSE` is an intentional safety-check configuration and does not mean product deadlocks are ignored.
- Mechanical equivalence between the Python implementation and the TLA implementation.

Next steps may refine the open-crash/reconcile model based on the implementation, but must not add fictional state machines for unknown remote caches.

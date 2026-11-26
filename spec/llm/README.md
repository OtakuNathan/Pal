# LLM model contract map

Models follow established runtime contracts, not whichever file has the most
recent timestamp. `scripts/check_llm_tla.sh` runs the seven current LLM models.
Historical cache models remain marked retired and are not reactivated.

## Endpoint invocation: selected endpoint only

`EndpointInvocationLifecycle` models one invocation after resolving a configured
endpoint. Its initial nondeterministic choice represents the possible selected
endpoints across runs, not permission to pick a different endpoint on failure.

- Model origin: `049e43c` (2026-08-01), which still allowed automatic fallback
- Current policy: `c0dd67d` (2026-10-05), “Require explicit idle model switches with
  replay compatibility checks”
- Runtime mapping: `LLMRuntime._enabled_endpoints_for_preference` returns only the
  selected endpoint; legacy fallback metadata cannot expand the list
- Contract: `docs/pal_llm_contract.md`, “Model switching and reasoning replay”
- Production regressions: `tests/test_llm_fallback_switch.py`,
  `tests/test_llm_sticky_fallback.py`, and `tests/test_model_switch.py`

The positive config sets `AllowFallback = FALSE`. Selection is immutable during
an invocation, retries increment attempts on that same endpoint, and endpoint
failure becomes terminal even if other endpoints exist. An explicit user model
switch occurs at the idle boundary and starts a new invocation; this model does
not prove replay compatibility or the switch's control-plane admission.

`EndpointInvocationLifecycleFallback.cfg` deliberately re-enables the old path.
It must fail `EndpointSelectionStable`. Do not include it in the positive runner.

## Turn history versus provider item commits

These are different abstractions, not interchangeable versions:

- `L1TurnLifecycle` owns turn-level calls/results, draft separation, protocol
  closure, and retention of result bodies plus reasoning/replay
- `ItemCommitLifecycle` owns provider item commit boundaries, terminal projection,
  and the execution eligibility of committed tool items

`L1TurnLifecycle.completeDrafts` means assembled drafts, not a provider commit.
Its `LengthOrBrokenTerminal` discards the uncommitted-draft abstraction. It must
not be used to conclude that a provider-committed tool item is discarded on
`length`. That obligation is explicitly checked by
`ItemCommitLifecycle.LengthPreservesCommitted` and
`SuccessfulTerminalPreservesCommitted`.

The intended mapping is:

1. Provider fragments are open item drafts, never executable merely because JSON
   looks complete
2. A recognized provider item boundary commits the item
3. A non-error terminal projects committed items; `length` drops open drafts but
   retains committed tool calls for the normal agent loop
4. The normal execution/result path supplies turn-level calls and results; stream
   decoding does not independently execute tools

Cancellation before terminal response acceptance abandons that assembled L1
response, including provider-committed items that have not entered the normal
execution path. Earlier accepted rounds/results remain unchanged; draft bytes
stay in private diagnostics. `tests/test_interrupted_response_atomicity.py`
checks unsigned thinking, signed thinking followed by a partial body, and restore
of the historical draft incorrectly marked complete. The two isolated models
do not prove this provider-item-to-L1 cancellation boundary; these are production
boundary regressions, not a new composition proof.

`LLMRuntime._recover_length` returns responses with committed tool calls without
regenerating them. This behavior and `ItemCommitLifecycle` arrived together in
`1d81086` (2026-08-05). The real wire regression
`test_length_terminal_preserves_only_provider_committed_tool_items` in
`tests/test_llm_ir_shapes.py` covers Responses and Anthropic item boundaries and
contrasts uncommitted Chat Completions fragments.

The later `c84871d` edit to `L1TurnLifecycle` (2026-09-23) changed settlement to
retain result bodies, reasoning and replay; it did not replace item-commit
semantics. Choosing the newer file timestamp would therefore be misleading.
This mapping is documentation of separate proof obligations, **not** a checked
TLA refinement/composition theorem. The turn model does not itself prove the
provider item-to-turn transfer.

## Cache model generations

`SessionChaseTail` is current. It models fixed session anchors, a bounded
recent lookup window and marker selection independent of transport admission,
turn changes and receipts. The `DropAnchors` mutant must violate `FixedAnchors`.
The old PromptCacheTail/PromptCacheAdmission models and their mutants are
historical (they described transport-owned marker history, which no longer
exists). Once-only transport accounting remains tested at the implementation
boundary independently of marker selection.

`CooperativeToolBatch` checks that a normal interrupt stops further model
requests while allowing the accepted batch to finish. Its early-close mutant
must violate `NoPartialClose`. These are isolated abstractions, not a proof of
composition across the runtime or external tool execution.

## Check

```sh
bash scripts/check_llm_tla.sh /path/to/tla2tools.jar
# Expected invariant failure, demonstrating the obsolete fallback path:
java -jar /path/to/tla2tools.jar -cleanup \
  -config spec/llm/EndpointInvocationLifecycleFallback.cfg \
  spec/llm/EndpointInvocationLifecycle.tla
PYTHONPATH=src python -m pytest -q tests/test_llm_fallback_switch.py \
  tests/test_llm_sticky_fallback.py tests/test_model_switch.py tests/test_llm_ir_shapes.py
```

`RoundSubmission` also models prospective native compaction of a continuous
accepted, protocol-closed RIGHT prefix. Capture leaves the live cut frozen; only
install transfers captured facts. The `early_native_cut` negative case detects
premature cut movement. Pending input and incomplete tool exchanges are outside
the model's accepted-fact abstraction and are covered by runtime regressions.

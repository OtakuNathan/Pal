# Result Guidance Contract — Tool Guidance Layering and Result-Side Affordance

Version: v2 implementation · 2026-09-24 · Task package `../pal-result-guidance/origin/` (v2.0)

## Three-Way Division of Responsibility

Guidance information is stored in three places according to "when it is needed", with three different owners responsible:

| Channel | Storage Location | Owner | When It Enters the Model View |
|---|---|---|---|
| Static capability understanding | descriptor (`purpose/use_when/do_not_use_when` + `NextToolHint`) | Tool author | Compiled into the stable description; displayed via the same source by `read_tool` |
| Failure recovery | Result-side `recovery_hint` + `affordances` | Business owner first; declared fallback as backstop | Only real failure results; the best single source is rendered |
| Action suggestion for this occasion | Result-side `affordances` (bound to real arguments) | Business owner judges based on the facts of this occasion | Only when supported by result-specific facts |

**Default behavior**: for successful results, `affordances=[]` and `recovery_hint=""`. Success, switching workspace,
the appearance of a path/ID, or seeing a tool for the first time — none of these constitute a reason to suggest. This is not "prompt on first use, deduplicate afterwards" — there is
no reason to prompt the first time either. Selection does not depend on any presentation history; there is no cross-turn seen set, prompt count, or TTL.

## Types and Fields

- `ToolGuidance.failure_next_steps: str = ""` — optional host-side fallback declaration. No longer enters the
  default model description (`compile_tool_description` does not project this field). Legacy non-empty declarations remain valid.
- `recovery_hint: str = ""` — short, this-occurrence repair guidance that cannot be represented as a full tool call. Runs through
  `CompleteResult/FailedResult/RejectedResult`, `ToolHandlerResult`,
  `ToolExecutionError/ToolRejectedError`, `CapabilityResult`.
- `affordances` — the existing `ToolAffordance(tool, arguments, reason)`; `CapabilityResult`
  gains this field (previously it was lost across normalization).
- Suggestions do not constitute approval, read authorization, snapshot ownership, or a new instruction hierarchy.

## Failure Best-Source Selection (§6.1 → `pal/execution/result_guidance.py`)

1. Host-side rejection before execution (unknown tool/mode/schema/target): keep the precise corrective affordance,
   do not append a business fallback.
2. Explicit handler guidance (recovery_hint/affordances) takes priority.
3. When there is no handler guidance, the declared `failure_next_steps` is filled into `recovery_hint`, rendered from a single source,
   no longer concatenated into the body, no longer giving an unconditional read_tool, and no longer appending a fixed recall_memory section.
4. No applicable fallback: keep only facts/effect/retry.

## Action Identity and Deduplication (§8 → `result_guidance.py`)

`key = (logical_alias, canonical_json(arguments))`; direct and `call_tool(name=...)`
wrappers are recognized as the same target; `read_tool(foo)` and executing foo are different actions; object key ordering, array order, and value
type differences are preserved; within a single result, deduplication keeps the first, most specific source. For captured generation, validate the alias,
input schema, target binding, and role scope, projecting legal invocation forms by direct/indirect, then take at most
3 valid actions. Bad candidates are dropped individually with diagnostics recorded, without overriding the business result. Normalization is idempotent.

## Sole Exit Point and Budget (§9 → `runtime._finalize_invocation_result`)

All `_invoke_tool_record_sync/_async` exits (early rejection, exceptions, builtin, call_tool
recursion) uniformly pass through the finalizer: candidate validation → body and recovery information budget.
Per the new rule confirmed by the user after review, optional `affordances` are presented in full outside the truncation budget,
neither crowding out body space nor being removed due to the body budget. The budget still covers the body, status/effect/error/retry,
recovery, and snapshot hints; the final string including optional suggestions is allowed to exceed the configured character count.
For native, normalize post-appending happens before the finalizer and follows the same rules.
Bad suggestions on CapabilityResult and exception carriers are dropped individually; when the failure guidance helper unexpectedly throws,
the original error/effect/retry/details are preserved and only the guidance fields are degraded.
`CancelledError` continues to propagate and is not disguised as an ordinary failure.

## Long-Line Hints Only on Real Reads (§10 → `_budget_invocation_result`)

Only when `manifest.operation == 'read'` and the file is not a managed snapshot is source-file shell view/modify guidance given;
the diff proof_length of edit/write is no longer treated as source line length; for long lines in managed snapshots, only viewing a bounded
fragment is hinted, with no modification suggestion.

## Migration Checklist (§4 Five-Way Classification)

| Source | Classification | Destination |
|---|---|---|
| Failure next steps section of `compile_tool_description` | FAILURE_FALLBACK | Moved out of the description; filled into recovery_hint on runtime failure |
| `prepare_lsp_workspace` ready-branch next_tools menu + direction tail | DROP_REDUNDANT | Remove (do not convert to typed); ready reports facts only |
| `prepare_lsp_workspace` partial/failed menu | RESULT_DYNAMIC | Migrate to a typed inspect_lsp_status affordance bound to the current workspace/server |
| LSP prepare/doctor/status static hints (use_when already conditional) | STATIC_RELATION_KEEP | Keep descriptor projection and search documentation |
| `prepare_call_hierarchy` incoming/outgoing static hints | STATIC_RELATION_KEEP | Keep; single-item results additionally get a location-bound continuation |
| `_render_invocation_for_llm`'s `_FAILURE_MEMORY_NEXT_STEP` tail | DROP_REDUNDANT | Remove; failures no longer unconditionally recommend recall_memory |
| `_default_failure_affordances` unconditional read_tool | DROP_REDUNDANT | Remove; when there is no fallback, only facts remain |
| `_append_failure_guidance` substring-dedup concatenation | FAILURE_FALLBACK | Change to a single source via recovery_hint |
| `details.failure_next_steps` setdefault | FAILURE_FALLBACK | Remove (recovery_hint is the only channel; structured does not enter the model body) |
| `shared/tool_routing.py` system prompt reference to "description's Failure next steps" | PRECALL_KEEP (rewrite) | Rewrite to reference result-side recovery/affordances |
| checklist/channel/browser status-conditional inline hints | RESULT_DYNAMIC (keep) | Triggered by real status conditions, not static replay |
| plugins/package_*, memory recall, web_search, etc. | No result-side additions | Already compliant; static hints kept |
| `turn_executor` timeout/rpc_failed direct synthesis | PRECALL_KEEP (bounded) | Truncate exception text head/tail under the same bounded presentation policy |
| bunshin `op_exec_shell` failure text "trapped, do not switch shell and retry" | PRECALL_KEEP (migrate) | Move into `do_not_use_when` (visible before invocation); failure side keeps only the recovery procedure |

## Result guidance examples (deliverable 4; stable descriptors always preserved,
"Preparation was partial or failed … inspect_lsp_status" conditional relationships)

The following is a summary of the shapes produced by actual egress:

1. **First prepare (A) → ready**: the body contains only readiness facts (202 characters, including status/servers);
   `affordances=[]`, `recovery_hint=""`, no `next_tools`, no direction tail.
2. **Cross-workspace second ready (B)**: exactly the same shape as 1; workspace_root/primary_server
   are kept as facts and do not constitute a recommendation reason; descriptor hash unchanged.
3. **Two workspaces each partial (B failed clangd / A failed clangd)**: each gets
   `affordances=[inspect_lsp_status(workspace_root=<that run>, name=<failed server>)]`,
   reason points to that run's not-ready facts; no mutual suppression (A's partial does not disappear because B's hint mentioned doctor).
4. **call hierarchy single item**: both affordances are bound to the item's
   file/line/character (+ optional workspace_root/name), pointing to incoming/outgoing;
   empty items do not construct; multiple items do not arbitrarily pick the first.
5. **ordinary failure (handler throws, no handler guidance)**: error facts + effect + retry
   as-is; `recovery_hint` = the declared fallback (appears only here); no read_tool push,
   no recall_memory tail.
6. **unknown effect (effect=unknown)**: retry=reconcile_first kept; advises not treating the original command
   as an unconditionally retryable action (`RetrySafetyPreserved`).
7. **saved-output failure (capture OSError)**: operation status/effect/receipt unchanged,
   `output_error` recorded faithfully; bounded preview + a "do not repeat side effects to retrieve output" hint;
   native-side output_ref/session_id recovery entry kept (command is not re-run).
8. **long-line source read**: single line exceeds budget and is an original-file read → conditional shell
   view/modify note (does not include read authorization); authorization still covers only the actually delivered range.
9. **snapshot read (managed snapshot)**: reuses the existing snapshot without copying; long lines only hint at viewing a bounded snippet,
   modification not suggested; no original-file edit authorization.

## Test matrix mapping (v2 110 scenarios → actual tests)

| Matrix group | Test location | Notes |
|---|---|---|
| A01-A10 | test_result_guidance.py::TestDescriptorLayering + test_tool_guidance.py (4, revised) | Description/search contract; A06 empty legality; A07 static projection; A09 same-generation stability |
| B01-B16 | test_result_guidance.py::TestFailurePickBest/TestDirectResultCarriers + test_immutable_tool_facade.py (revised) | Pick-best/fact fidelity; B15 five-carrier parameterization; B12 guaranteed by finalize's exception degradation branch |
| C01-C14 | test_result_guidance.py::TestActionIdentity/TestResolveFailureGuidance | Key normalization/dedup/idempotency; C08 covered by finalize's generation filtering + bunshin role-code verification; C09-C10 covered by generation capture semantics |
| D01-D20 | test_result_guidance_budget.py (all) | Both P2s; D13 authorization preservation covered by existing test_result_snapshots.py; D18 snapshot long line = managed branch |
| E01-E10 | test_result_guidance_h.py::TestProviderLevel/TestEndToEndSequence | LSP decision table + other producers; E06/E08/E09 covered by the "no result-side additions" fact + B11/E01 generalization |
| F01-F12 | F01 nullable defaults (wire round-trip test includes legacy payload branch); F02 `test_wire_roundtrip_preserves_guidance_fields` (TOOL_INVOCATION_RESULT_ADAPTER JSON round-trip); F03 frozen history is not re-rendered; F04/F05 existing snapshot/L1 tests; F06/F07/F10 native suite (151 tests OK, including uncommitted lease assertions); F08 events/observation changed to deliver_invocation_result; F09 bunshin role code; F11 cancellation semantics untouched; F12 native uncommitted diff fully preserved | |
| G01-G07 | G01/G02 = TestActionIdentity/existing snapshot sentinel tests; G03 = offline comparison in this document; G04 search hit consistency; G05 = four CI batches + native suite; G06 = this document; G07 migration checklist (above) | |
| H01-H22 | test_result_guidance_h.py (H01-H05/H07/H09-H11/H13/H14/H20/H21 explicit; H06 no historical state = H20 + no serialization; H08 = H21; H12 = `TestScopedRoleFiltering` (out-of-role actions filtered + static relation downgraded); H15-H18 = producer no-result-side-additions fact + B11/E01 generalization; H22 = this workspace was implemented from a clean baseline, v1 never started, not applicable) | |
| B12/D12 supplements | B12 = `TestGuidanceFailureDegrades`(forces helper exceptions, fact fidelity degradation); D12 = `test_unicode_emoji_bom_budget_maps_spans_not_decorations`(emoji/CJK/BOM budget and authorized range mapping) | |

## Initial-version CI and regression records (§13, historical execution records)

The following are initial-version implementation records and do not reflect CI results after the review revisions. The review revisions only ran targeted tests,
and did not rerun CI; among these, budget assertions were updated to "body and recovery information are bounded, optional actions presented independently".

| Batch/suite | Result |
|---|---|
| core-a (final verification after fixing 2 assertions pinned to old behavior + adding B12/H12/D12 residual tests) | **1205 passed**, 5 skipped (first run had 1200+2 failed: `test_minimal_operating_rules_prompt_omits_route_specific_tools` phrase recovery, `test_read_tool_full_contract_is_retained_only_in_structured_channel` inverted per A01/A03) |
| core-b (final verification including D12 residual tests) | **1119 passed**, 2 skipped |
| bunshin-a | 464 passed (including bunshin A02 migration verification) |
| bunshin-b | 477 passed |
| native tests/pal_host (unittest) | 151 tests OK (including uncommitted lease/output_ref assertions; test_remote's isolated SSH E2E depends on sshd, the suite has its own skip) |
| Focused regression (6 guidance-related files) | 85 passed |
| AGENTS.md-specified test_tool_activity.py | 6 passed |

Not run/restricted: test_remote's isolated SSH E2E requires sshd (the suite has its own skip marker); native requires a local venv + dist wheel to provide the C++ extension.

| Metric | before | after |
|---|---|---|
| Provider description total characters | 9,186 | 7,460 (-19%) |
| read_tool (prepare_lsp_workspace) characters | 637 | 280 (-56%) |
| Ordinary success result final characters | 15 | 15 (unchanged, zero suggestions) |
| 50KB exception final model-visible characters | 50,727 (**unbounded**) | 688 (bounded) |
| Failure's fixed recall_memory tail | present | removed |
| prepare(A)→ready final characters | 680 (with menu wording) | 202 (zero menu wording) |
| prepare(B)→ready | 680 (with menu wording) | 202 (zero menu wording) |
| prepare(B)→partial | 0 typed suggestions (menu prose) | 1 inspect_lsp_status bound to B/clangd |
| prepare(A)→partial | 0 (same as above) | 1 bound to A/clangd |
| descriptor hash (within a sequence) | stable | stable |
| Fixed search query hits | — | item-by-item identical to before (static relationships preserved) |

Measurement script logic: same-generation fake LSP RPC, `ToolCallBudget(max_output_chars=2000)`,
counting a single handler call. These figures only demonstrate projection/budget/deduplication-control changes, and do not support claims about real model
call rounds or end-to-end latency reduction.

## Invariants (property-test constraints)

See `tests/test_result_guidance*.py`: operational fact fidelity, no execution side effects, suggestions validated via captured
generation, unique action keys per result, deterministic selection, normalization idempotence, no static replay on success,
ready prepare has zero recommendations, no history-based suppression, descriptor stable within a generation, body and recovery information bounded.

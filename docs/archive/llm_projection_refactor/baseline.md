# LLM Projection Refactor v2 — Baseline Record

> Historical record, archived on 2026-10-05 from `docs/llm_projection_refactor/baseline.md`.
> Statements about implementation, deployment and test results describe the
> original work, not the current checkout or running host. See the
> [current documentation index](../../README.md).

**Date: 2026-09-18. Executor: Pal.**
**Baseline commit: `7e0b1f74cbbedc3214ba362c9b1413ae058aebe4` (= origin/main, verified identical).**
**Branch: `refactor/llm-projection-session-v2`. Worktree: `~/Documents/coding/Pal-projection-v2`.**
**Protection branch: `safety/llm-projection-before-v2-7e0b1f7` (local).**
**Plan package: `~/Documents/coding/upgrade/pal_projection_plan/`, sha256sum -c MANIFEST.sha256 all OK.**

## Environment

- Host: Raspberry Pi (this machine), memory 3.7Gi
- Python 3.13(`~/.local/lib/python3.13`), pytest 9.0.3
- Java OpenJDK 21.0.10; `tla2tools.jar` already exists at `~/Documents/coding/Pal/tla2tools.jar` and `~/tla2tools.jar`
  → **Different from the plan package author's environment: SANY/TLC can run on this machine, so the P1 gate has no tooling obstacle**
- ⚠️ Worktree import trap: the system editable install resolves `import pal` to the main checkout
  (`~/Documents/coding/Pal/src`). **All tests/scripts inside the worktree must
  be prefixed with `PYTHONPATH=$PWD/src`**, otherwise old code is silently tested. Both resolution paths have been verified in practice.

## Confirmation conclusions on the plan's three pre-check divergence points (required by HANDOFF.md)

### 1. How existing durable commits atomically associate the IR with required-native

Current state: **there is no per-turn joint commit**. Chain of facts:

- `MemoryRuntimeStatePort.snapshot_state()` (`src/pal/memory/runtime_state.py:34`) serializes
  L1 turns as a whole; each message goes into the payload via `message_to_payload()` **together with its ReplayEnvelope (i.e., the v1 native)**; L2 entries, heat, and top-of-mind are in the same snapshot.
- `prepare_restore_state()` performs full validation, then `install_prepared_state()` replaces in one shot — **the atomicity granularity = the entire runtime state snapshot**.
- `MemoryService._close_l1_turn_transactionally` (`src/pal/memory/service.py:494`) is an **in-memory** transaction (replace + rollback to `restore_turn` on failure) and produces no persisted receipt.

Inference: PLAN §4's `HistoryCommitReceipt` (semantic/native committed under the same receipt) is a **new construction**;
no directly reusable per-turn primitive exists; the boundary P4 should reuse is the snapshot/install full-volume pair.

### 2. Whether the context-view normal turn boundary has early content rewriting

Conclusion: **No rewriting of earlier history found on the normal path; two bounded turn-boundary projection-change channels exist**:

- `projected_messages()` (`src/pal/memory/context_view.py:15`) only filters, without rewriting content:
  1. After settling, the projection of **settled turns** drops the pal-authored `pal_prompt_context` developer message and summary projection (`settled=True` branch, call sites `turn_executor.py:1122/2091`, `turn_ir.py:487`) — these blocks sit at the tail of the turn, so the frozen prefix is unaffected (pinned by `test_prompt_prefix_stability_e2e`);
  2. the `turn.metadata["prompt_context_state"]["excluded"]` set can explicitly drop messages — a **bounded rewriting channel**.
- Response to PLAN §5.3: the host does indeed have a mechanism whereby "content already sent disappears in subsequent requests", but its scope is limited to transient tail-of-turn blocks and the explicit exclusion set; the new design must elevate these two channels into explicit receipt/generation events and must not silently take the append path.

### 3. Verify provider continuation constraints against the actual endpoint configuration

The 11 currently enabled endpoints span all three shapes (see details in
`continuation_contract_matrix.md`). Key fact: **the active endpoint `deepseek-v4.1-flash`
uses anthropic_messages**, so the [W2]/[W3]-class thinking/signature/reasoning_content contracts
directly constrain the production main path. GLM's (openai_completion) continuation requirements were not verified; marked TBD;
the openrouter GPT-5.6 three endpoints and gpt-6-astra use openai_response, so the [W1] encrypted_content
contract applies (astra's cache-route decision is subject to a separate Nathan 2026-09-17 hold decision, not part of this round's rollback).

## Local verification record (listing only what was actually executed)

| Check | Result | Date |
|---|---|---|
| Plan package sha256 verification | All OK | 2026-09-18 |
| Repository baseline (after fetch, HEAD=origin/main=7e0b1f74, working tree clean) | Identical | 2026-09-18 |
| Plan package examples, 14 constructed tests | 14 passed (re-run locally, consistent with evidence in the package) | 2026-09-18 |
| Full suite pytest tests/ (2823 tests / 145 files) | See next section | 2026-09-18 |

### Full suite results

**Run-mode change (P0 finding): the full suite cannot complete in a single process; switched to per-file isolated runs.** Chain of facts:

1. Foreground single-process attempt: not finished after ~30 minutes, manually terminated (no progress visibility, a process mistake);
2. Background single process (systemd transient service + log): the first 395 items completed in 81 seconds, then it entered the
   `test_bootstrap_and_repositories.py` (136 tests) slow zone at ~15s/test, with pytest
   RSS growing linearly (219MB → 426MB, available memory 1.8Gi → 406Mi);
3. Proactively terminated before endangering the host; the completed portion was **454 passed / 0 failed** (saved
   `/tmp/pal_baseline_partial_450.log`);
4. From this, the mechanism of this morning's CI crash is inferred: the suite's slow zone occupying for a long time + ollama loading models stacked → memory exhaustion hard hang (not a single OOM kill; logs did not get written to disk in time).

**Final strategy**: `/tmp/pal_baseline_chunked.sh` — an independent pytest process per test file
+ 1500s per-file timeout, with the summary written to `/tmp/pal_baseline_chunks/summary.tsv`.
RSS growth is contained within a single file, and slow/heavy files are exposed individually.

**Chunked results (141 test files run in isolation one by one, no timeouts):**

| Item | Value |
|---|---|
| Files with rc=0 | 139 / 141 |
| Tests passed | 2557 (excluding subtests count; subtests in multiple files all passed as well) |
| Failures | **1**: `test_package_installation.py::test_conflicting_dependencies_and_repeated_preparation_are_isolated` |
| Skips | **1** (module-level): `test_tool_schema_properties.py` — missing the `hypothesis_jsonschema` dependency |
| Timeouts (rc=124) | 0 |

**Characterization of the only failure: a pre-existing environmental failure, unrelated to this refactor.** Evidence: it fails the same way when rerun standalone in the main
checkout (~/Documents/coding/Pal, same PYTHONPATH=src);
the assertion is `sys.path == before_path`, polluted by import-time injection from the host's `/usr/lib/python3/dist-packages/
setuptools/_vendor` (the jieba → pkg_resources chain). Fixing it
is out of scope for this project; it is recorded in the baseline and not pursued further.

Conclusion: **the baseline is ready to build on** — apart from the 1 environmental failure and 1 dependency-missing skip above,
everything passes. The comparison baseline for subsequent gates is this table.

## Deviations from PLAN (incremental, no plan changes)

1. This machine has tla2tools.jar + Java 21 → PLAN §9 "current verification status: TLC not yet executed" can be lifted on this machine; the P1 gate runs SANY/TLC + mutants as usual.
2. The worktree requires PYTHONPATH to be prepended (see the environment section).
3. The full suite takes roughly the time shown above on this machine; subsequent gates should always run in the background with logs.

## Constraint Compliance Statement

No production database/configuration was modified (`~/.pal/pal.sqlite3` was only read via SELECT for endpoint metadata, containing no secrets);
the production Pal was not restarted; no real keys were used; no paid calls were made; no force push; main untouched.

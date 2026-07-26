# TODO: Logical-session execution state

Status: implemented.

Implementation:

- `pal.execution.session_state` owns the backend-neutral clock, pager manifest,
  delivery-range, and context-epoch contracts.
- Host execution uses the in-memory reference backend. Minion role workers use
  `RoleGatewayLogicalExecutionState`; the Manager persists its canonical
  envelope on `minion_v2_role_sessions` and stores validated result payloads as
  `LogicalToolResultArtifact` objects.
- Agent-session checkpoint schema 3 preserves the tool-delivery journal while
  the Manager remains authoritative for clocks, handles, and file grants.

## Goal

Move pager lifetime and file-read visibility/read-state ownership from a
physical worker process to the logical role session. Endpoint retries, worker
respawns, hot reloads, and plugin detach/attach must not change their
semantics.

## Session clock

- The Manager owns and durably persists `logical_session_id` and
  `current_user_turn`.
- A new logical input increments the counter exactly once. The transition must
  be idempotent by input/message ID so recovery cannot count the same input
  twice.
- In a Minion role session, logical inputs include the initial assignment, a
  new repair bill/candidate assignment, and a user clarification or revision
  delivered to that role.
- LLM continuations, tool calls, pager reads, provider retries, worker attempts,
  process restarts, and hot reloads do not increment the counter.

## Pager contract

- A result handle belongs to exactly one `logical_session_id`.
- A handle created at user turn `N` has `expires_at_user_turn = N + 5`.
- It is available only while
  `current_user_turn < expires_at_user_turn`; reaching the expiry turn retires
  it mechanically.
- Reading a page does not refresh or extend the lifetime.
- Completing, cancelling, or abandoning the logical session retires all of its
  handles immediately.
- Persist the validated serialized result/paging payload plus the handle
  manifest. An in-process `ToolResultPager` is only a hot projection.
- Registry-generation changes and capability detach must not invalidate
  already materialized results. Paging reads stored validated output and never
  re-executes the original tool.
- An expired handle returns a structured `expired_handle` result with a
  recovery affordance to rerun the originating tool. It must not degrade into
  an artifact reference or partial untagged text.

## File context integration

- Pager state and file-read state use the same logical session clock and
  persisted session envelope.
- Record only file content actually delivered to the model:
  - `complete` records the complete delivered range;
  - `paged` records only the current page;
  - each successful `read_tool_result` extends the delivered ranges.
- Undelivered or expired pages never authorize a later edit.
- Pager expiry removes access to unread result content but does not erase
  ranges still visible in the current model-context epoch.
- If context compaction/reconstruction removes earlier file content, begin a
  new visibility epoch. A still-live pager may deliver the page again and
  record it in the new epoch.
- File identity must use one canonical filesystem key so `~`, absolute paths,
  symlink-resolved paths, and normalized equivalent paths cannot disagree
  between read and edit checks.

## Recovery and persistence

- Hydrate pager manifests, payload references, file identities, digests,
  delivered ranges, and the current context epoch when a logical role session
  resumes in another worker.
- Remove process-global correctness dependencies and retry-only bypasses.
  Process-local caches may remain only as disposable accelerators over the
  durable session projection.
- Scope every lookup by `(logical_session_id, handle_or_file_key)` so state
  cannot leak across roles, workflows, or worktrees.

## Required tests

- A provider retry and worker respawn preserve handles and delivered file
  ranges without advancing the user-turn clock.
- Replaying the same logical input ID does not advance the clock twice.
- A handle works through turns `N` to `N + 4` and is expired at `N + 5`.
- Page reads do not extend expiry.
- Session termination immediately expires every handle.
- A registry-generation swap does not break an existing materialized handle.
- Complete and paged file results authorize only the ranges actually delivered.
- A new context epoch does not inherit invisible ranges, while a live handle can
  reveal them again.
- Equivalent `~` and absolute paths share one read/edit identity.
- Handles and file state are inaccessible from another logical session.

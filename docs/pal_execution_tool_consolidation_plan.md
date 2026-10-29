# Execution Tool Surface Consolidation Plan

Date: 2026-10-15
Status: Implemented into source code per the confirmed scope; see [Implementation Notes](pal_tool_consolidation_implementation.md) for targeted verification. Not yet installed or activated.

## Design Principles

Continuing from the [Browser tool surface consolidation plan](pal_browser_tool_consolidation_plan.md): prioritize reducing duplicate entry points; simple enums may be merged, but do not cram multiple sets of operation-specific parameters into a single mega-tool.

Shell retains independent, explicit everyday operations. Tool descriptions explain purpose and preconditions; when a session appears, provide brief, relevant guidance rather than repeating the full session-management manual on every output.

## Current Baseline

By execution capability and current native shell / remote shortcut declarations, there are 24 entry points and 8 resident direct-call tools. This is a source-code and current-configuration accounting, not an online-instance snapshot; the current remote configuration generates one `run_shell_desktop` shortcut entry.

| Group | Tools | Count |
| --- | --- | ---: |
| Tool discovery, invocation, and registration status | `search_tools`, `read_tool`, `call_tool`, `list_tools`, `inspect_execution_state` | 5 |
| Files | `read_file`, `edit_file`, `write_file`, `delete_path` | 4 |
| Shell execution | `run_shell`, `run_shell_desktop` | 2 |
| Shell sessions | `manage_shell_session`, `read_shell_session`, `write_shell_session`, `resize_shell_session`, `terminate_shell_session`, `release_shell_session`, `watch_shell_session`, `extend_shell_session`, `unwatch_shell_session` | 9 |
| Shell status | `inspect_shell_status` | 1 |
| Remote | `list_remote`, `start_remote_target`, `shutdown_remote_target` | 3 |
| Total | | **24** |

## Confirmed Entry Points to Withdraw

### delete_path

Withdraw the model-callable dedicated path deletion tool; file deletion is handled by existing shell capability; no equivalent replacement tool or searchable legacy alias will be kept for this.

During implementation, inventory references in tool declarations, recovery prompts, role projections, documentation, and regression tests. Whether the internal path-handling code has other consumers is checked separately before deciding the cleanup scope.

The existing deletion tool has expected hash, path type, and immutable output snapshot checks; we cannot claim shell deletion provides the same structured contract; snapshot references and lifecycle in the runtime are still managed by the original owner.

### manage_shell_session

Withdraw the model-callable eight-operation mega entry point. Keep the simple tool entry points for read, write, resize, terminate, release, watch, extend, and unwatch; for now, do not reassemble some of them into a new management tool.

This only withdraws the model tool surface; it cannot directly delete the internal `session_async` dispatch, state verification, and output recovery implementations shared by the independent tools.

Update the next-tool guidance in `run_shell`, session results, status checks, built-in skills, and other tools so they no longer recommend the withdrawn entry points.

Prompt conventions:

- Static descriptions of `run_shell` and related tools explain how to read, feed input to, or terminate as needed after a `session_id` is returned.
- When a session is returned for the first time, or a new state requiring action occurs, give brief and relevant guidance as needed.
- Do not repeat all tool names, parameter tables, and lifecycle explanations on every result.
- When output export fails, continue to give the accurate `read_shell_session` recovery entry point; retain the `output_ref`; do not require rerunning the producing command.

## list_tools: Absorbed by the Existing Domain Directory

Currently, `list_tools` returns the names, purposes, owning modules, and invocation modes of all registered tools at once, for a global inventory browse.

When `search_tools(domain="shell")` is called without a query, it already provides paginated directory listings for that domain, at most 16 names and brief purposes per page, supporting `directory_offset` / `next_offset`. This kind of directory navigation differs from the full candidate contract for relevance search; the latter continues to default to at most 3 candidates, capped at 5.

Recommendation: withdraw the separate `list_tools` entry point; unify on discovering the domain first, then browsing that domain's directory; do not stuff the full tool contract into search results. During implementation, confirm that the existing return with no query and no domain can provide a visible domain list; inventory the callers and tests of the original global listing; any required administrative statistics or full exports can continue to use the internal registry / CLI.

The directory is only for navigation: after selecting a candidate, read the full contract per existing rules. Preserve tool visibility and role authorization boundaries; do not expose tools the caller is not authorized to invoke just because the entry point was replaced.

## Meaning and Handling of release

`release_shell_session` is for actively discarding the retained output of a completed session, or abandoning results not yet successfully exported via `output_ref`. Running sessions cannot be released; canceling the process is the responsibility of `terminate_shell_session`.

The earlier "then join release" meant adding it to a lifecycle-management enum. Per this round's direction of withdrawing the mega entry point and keeping simple tools, that merge is not adopted for now; release remains independent.

## Boundaries Explicitly Kept

- `read_file`, `edit_file`, and `write_file` are respectively responsible for reading, precise partial replacement, and full content writing; each keeps its own read versions and preconditions.
- `read_shell_session`, `write_shell_session`, and `resize_shell_session` are not merged, avoiding mixing parameter contracts for output, PTY text, and terminal size changes.
- `watch_shell_session` schedules background decision notifications; `extend_shell_session` extends an existing finite deadline; `unwatch_shell_session` turns off active notifications; all keep their original semantics.
- `start_remote_target` and `shutdown_remote_target` stay separate. Their current use cases are startup/WOL and managed shutdown respectively; startup implementation still follows the configuration actions declared by the target; the two are not stitched into a new operation enum.
- `list_remote` keeps target summaries, specified target details, and on-demand refresh; no new fine-grained status tools are added.
- `inspect_execution_state` handles registered-tool and generation diagnostics; `inspect_shell_status` handles sessions and retained-output inventory, preserving the distinction in diagnostic subjects.
- `search_tools`, `read_tool`, and `call_tool` keep the division of responsibilities among discovery, contract reading, and execution.

## Suggestions Not Yet Individually Confirmed

`run_shell_desktop` is a resident shortcut projection with a fixed target; it could be considered for withdrawal and unified into `run_shell(target=...)`. Before withdrawal, verify role projections, fixed-target constraints, and target discovery flows; it is not currently regarded as a confirmed withdrawal item of this round.

## Counts and Verification

- Confirmed withdrawal of delete_path and manage_shell_session: 24 → 22, resident count unchanged.
- If the list_tools directory-absorption plan is adopted: 22 → 21, resident count unchanged.
- If the remaining remote shortcut is later revoked: 21 → 20, resident 8 → 7.

During implementation, verify the ownership and contribution guidelines of both the Pal repository and the `pal-shell-native` repository separately, and preserve existing uncommitted changes. Targeted verification is needed for tool visibility, old alias revocation, independent session operations, session recovery, identity restoration after compact, domain directory pagination, and role permissions.

Model evaluation checks discovery, correct tool and parameters, actual execution results, duplicate prompt volume, and number of call rounds; improvement is not judged solely by a reduced name count. Full CI is left to GitHub; code changes, installation file copies, and running-instance activation are each recorded separately.

# Pal Minion V1 Implementation Notes

This file is the current-code sync point for the first implemented minion subsystem.

## Boundary

`minion` is a detachable first-party plugin/module. Its builtin plugin manifest lives under `runtime_root/plugins/_builtin/minion`, while plugin-owned runtime configuration lives under `runtime_root/plugins/minion`.

PalCore does not own tasking tables, work order rules, checkpoint semantics, approval decisions, or natural-language task matching.

PalCore only sees:

- a minion event source
- minion capability providers
- a registered control action handler

Detach removes all three surfaces. Minion business state remains owned by the minion subsystem.

## Approval Flow

Minion runner emits `approval_requested`.

`MinionEventSource` converts it into `EventKind.APPROVAL_REQUEST`.

`ControlPlane` converts it into a generic `approval_requested` action.

PalCore sends a generic `interactive_open` channel interaction. Telegram renders the inline keyboard. Channel-specific fallback belongs in channel endpoints, not in minion business logic.

Button clicks produce `minion_approval_decision`. PalCore routes that action through `ControlActionHandlerRegistry`. The minion module handler calls manager RPC `send_decision` and records the ledger entry.

`op_minion_decision_send` is intentionally not exposed to the LLM capability surface.

## Tasking Store

The minion subsystem owns these runtime tables in `pal.sqlite3`:

- `minion_tasks`
- `minion_work_orders`
- `minion_work_order_drafts`
- `minion_work_order_milestones`
- `minion_worker_checkpoints`
- `minion_worker_ledger`
- `minion_task_lessons`
- `minion_system_lesson_candidates`

It also owns FTS tables for task and work order search:

- `minion_tasks_fts`
- `minion_work_orders_fts`
- `minion_work_order_drafts_fts`

One task can have only one active work order. This is enforced by the minion repository and a partial unique index over active work order statuses.

## Work Order Drafts

Work order drafts are minion-owned planning artifacts. They capture user brainstorming, module boundaries, proposed milestones, acceptance criteria, and workspace hints before the subsystem creates a formal work order.

Drafts are not progress facts and are not PalCore state. They are searchable so Pal can say "use that draft" without relying on chat context.

The intended route is:

1. Pal captures user brainstorming with `op_minion_draft_work_order`.
2. Planner reviews the draft as a bounded input.
3. Pal/user confirms the reviewed candidate.
4. `op_minion_promote_work_order_draft` records the formal work order.
5. `op_minion_spawn` can then start the selected profile from that work order.

Planner must not invent task boundaries from raw chat history when a draft exists. The draft is the bounded source for review.

## Checkpoint Cursor

Work orders contain ordered milestones.

A checkpoint is the cursor fact. There is no separate resume cursor field.

- `status=completed` advances the derived current milestone.
- `status=partial` and `status=blocked` record the scene but do not advance the cursor.
- The current milestone is derived as the first milestone without a completed checkpoint.

Pal must not infer work order progress from chat text, progress text, or terminal summaries. The fact source is the minion tasking store.

## Git Task Environment

Minion work happens inside a task environment, and the task environment is a Git repository.

- A task owns or resolves a task repo.
- A work order runs on its own branch in that repo.
- A milestone completion is represented by one Git commit on the work order branch.
- A completed checkpoint records the milestone index and the corresponding commit SHA.

This keeps the minion ledger and workspace state aligned: the minion tasking store knows which milestone is complete, and Git can restore the files for that milestone.

Spawn must read or initialize the task repo before starting a runner. For an existing work order, spawn must checkout or create the work order branch from the recorded base ref. A runner must not do task work on Pal's live branch unless that workspace was explicitly assigned as the task repo.

When a runner finishes a milestone:

1. Stage the milestone's task-scoped changes.
2. Commit them to the work order branch.
3. Emit `checkpoint(status=completed, milestone_index=..., commit_sha=...)`.

If there are no file changes for a milestone, the checkpoint may record `git_commit.status = "no_changes"` and still advance the milestone when the milestone's acceptance criteria are satisfied.

If commit fails, the runner must emit `partial` or `blocked`; it must not emit a completed checkpoint for that milestone.

After user acceptance, the minion subsystem may squash the milestone commits into one final work-order commit and merge or apply it to the configured target branch. This finalization is a separate control operation from milestone checkpointing.

## Capabilities

The minion module exposes:

- `intro_task_search`
- `intro_task_read`
- `intro_work_order_search`
- `intro_work_order_read`
- `intro_work_order_draft_search`
- `intro_work_order_draft_read`
- `intro_minion_list`
- `intro_minion_read`
- `intro_minion_profile_list`
- `intro_minion_profile_read`
- `op_minion_draft_work_order`
- `op_minion_promote_work_order_draft`
- `op_minion_spawn`
- `op_minion_kill`
- `op_minion_finalize`

`intro_work_order_read` returns work order status, milestones, derived current milestone, latest checkpoint, latest completed checkpoint, recent ledger, current worker, task lessons, and pending system lesson candidates.

`current_worker` comes from manager active runs bound to the work order. It is not inferred from conversation context.

`op_minion_spawn` may accept `task_query` instead of `work_order_id`. The minion repository resolves it through work order search only when there is exactly one candidate. Multiple candidates or no candidates return facts for Pal to show or ask about; Pal must not guess.

`op_minion_spawn` may also accept `draft_id`. In that case the minion subsystem promotes the draft into a formal work order first, then spawns from the resolved work order. This keeps the LLM-facing entry point small while keeping draft promotion explicit and testable.

Natural-language minion control should resolve facts first. For requests like "what is it doing?", "replace it", "continue this task", or "merge the completed work", Pal should inspect active runs and work order snapshots, then call the relevant operation. Pal must not infer current worker, progress, or milestone completion from conversation text.

## Spawn Continuity

`op_minion_spawn` resolves the requested profile, applies role defaults and capability exposure hooks, then asks the minion repository to prepare continuity.

`TaskContextPack.continuity` includes:

- current milestone
- completed milestones
- latest checkpoint
- latest completed checkpoint
- recent ledger
- task-wise lessons

The manager repeats this preparation before launching the runner, so reattach/retry uses live minion-owned facts.

## Profiles

Minion profiles are declarative. Builtin profiles are package TOML templates, and runtime profiles are loaded from `runtime_root/plugins/minion/profiles/*.toml`.

Current builtin profiles are `generic`, `planner`, `coder`, and `reviewer`. `planner` owns both planning and architecture review: work-order decomposition, module/interface boundaries, design tradeoffs, risks, migration notes, and verification steps.

Profile resolution order is builtin templates, runtime profile files, then currently mounted provider declarations. Later declarations with the same `profile_id` override earlier ones.

Profiles may declare capability groups. `core_minion_read` includes scoped discovery/read/call plus read-only `op_l3_recall_query`, so runners can recall relevant experience without memory-write access. `web_research` expands to `op_web_search_query` and `op_web_fetch_read`, so research-capable minions reuse Pal's existing web tools instead of owning a second web integration.

Profiles may also use `capability_policy.mode = "inherit_filtered"`. In that mode, spawn starts from Pal's current capability registry, adds profile defaults and provider hook results, then applies the minion deny policy. Explicit `TaskContextPack.allowed_capabilities` still wins for tightly scoped runs.

The runner consumes only the resolved profile snapshot in `TaskContextPack`. It does not query PalCore for profile policy after spawn.

## Runner Capability Boundary

The runner is allowed to execute task capabilities, report milestone events, and request approval. It must not manage Pal or the minion subsystem.

Profile resolution applies a default deny policy before the runner starts. The runner also rechecks the same policy before exposing tools or executing tool calls.

Denied by default:

- all `intro_*` capabilities
- all `op_minion_*` capabilities
- memory writes such as `op_l3_commit_write` and `op_l3_correct_patch`
- behavior, skill, channel, plugin, lifecycle, attach/detach/rescan/enable/disable style operations

This prevents recursive spawn/kill/list/read behavior. A task runner does not need to know the minion control plane exists; Pal observes and manages that layer through the minion module capabilities.

## Runner Loop

The runner is a thin execution entity, not a forked Pal. It starts a slim runtime with LLM, execution, artifact metadata, read-only L3 recall, and allowed task tools such as shell/code execution and web search/fetch. It does not load channel endpoints, service triggers, control panel, or Pal user-facing routing.

`TaskContextPack.allowed_capabilities` is the internal allowed pool. To keep token cost low, the normal LLM tool surface exposes only a small resident work set: `op_exec_disc_search`, `op_exec_disc_read`, `op_exec_capability_call`, `op_exec_run`, `op_web_search_query`, `op_web_fetch_read`, and `op_l3_recall_query` when those capabilities are allowed. Discovery runs through a scoped execution view, so denied or non-allowed capabilities cannot appear in search/read results.

Tool calls are executed through the existing `ExecutionRuntime` path and must be present in `TaskContextPack.allowed_capabilities`.

If a tool or capability call fails and read-only `op_l3_recall_query` is allowed, the runner prompt requires recall of relevant prior experience before retrying, debugging further, or reporting the milestone blocked.

High-risk calls declared by the task approval policy pause the runner and emit `approval_requested`. Reject or edit decisions block the milestone; accept continues the tool call.

## Lessons

Terminal minion events may carry:

- `task_lessons`
- `system_lessons`

Task lessons are stored as tasking continuity. System lessons are stored only as pending candidates. They are not written to L3 memory or committed as skills without a later confirmation flow.

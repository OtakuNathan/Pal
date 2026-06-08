# Pal Minion V1 Implementation Notes

This file is the current-code sync point for the first implemented minion subsystem.

## Boundary

`minion` is a detachable first-party plugin/module. Its builtin plugin manifest lives under `runtime_root/plugins/_builtin/minion`, while plugin-owned runtime configuration lives under `runtime_root/plugins/minion`.

PalCore does not own tasking tables, work order rules, checkpoint semantics, approval decisions, or natural-language task matching.

PalCore only sees:

- a minion event source
- minion capability providers
- registered control action handlers

Detach removes all three surfaces. Minion business state remains owned by the minion subsystem.

## Approval Flow

Minion runner emits `approval_requested`.

`MinionEventSource` converts it into `EventKind.APPROVAL_REQUEST`.

`MinionControlEventHandler` asks `pal.control.interactions` to compile a typed `interactive_open` delivery and attaches it to a control action. PalCore sends that delivery through the current route. Telegram renders the inline keyboard. Channel-specific fallback belongs in channel endpoints, not in minion business logic.

Button clicks use `control.action.dispatch` and produce `minion_approval_decision`. PalCore routes that action through `ControlActionHandlerRegistry`. The minion module handler calls manager RPC `send_decision` and records the ledger entry.

`op_minion_decision_send` is intentionally not exposed to the LLM capability surface.

## Event Delivery And User Notifications

The manager sidecar owns runner lifecycle and event publication. Pal subscribes to manager events over IPC. The provider buffers pushed events and calls `core.notify_ready()`, so completion can wake PalCore without waiting for the next user message.

Event delivery is split by audience:

- `progress` is high-cardinality telemetry for manager/tasking state. It is not a chat notification.
- `checkpoint` is the milestone cursor fact. It is recorded in the tasking store and notifies no one directly.
- `terminal` is the user-facing completion path. It sends a short route reply to the original control route when one exists, and synchronizes a recent completion observation into Pal's prompt context.
- `work_order_completed` is the work-order completion path. It is emitted when manager ledger state closes the active work order, so Pal and the user do not need to poll to discover that the order finished.

Terminal notifications should name the status, profile, work order, and up to a few artifact paths. Large plans, review reports, and writeups belong in minion artifact files, not in chat text.

Pal should answer "what is it doing?" or "where is it now?" by reading minion active-run and work-order facts, not by relying on previously pushed chat text.

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

Work order drafts are minion-owned planning artifacts. They capture user brainstorming, module boundaries, proposed milestones, acceptance criteria, and workspace hints before the subsystem creates a formal work order. Draft milestones are review input only; they are not an executable milestone truth source.

Drafts are not progress facts and are not PalCore state. They are searchable so Pal can say "use that draft" without relying on chat context.

The intended route is:

1. Pal captures user brainstorming with `op_minion_draft_work_order`.
2. Planner reviews the draft as a bounded input.
3. Pal/user confirms the reviewed candidate.
4. The reviewed output is written as a `FinalPlanArtifact` and accepted as a `plan_ref`.
5. `op_minion_spawn` starts the selected profile from the accepted plan or from a work order already bound to an accepted plan.

Planner must not invent task boundaries from raw chat history when a draft exists. The draft is the bounded source for review.

## Checkpoint Cursor

Executable milestones come from one source: an accepted `FinalPlanArtifact`.

Work orders contain the materialized milestone cursor derived from that plan. They do not define their own raw executable milestones.

A checkpoint is the cursor fact. There is no separate resume cursor field.

- `status=completed` advances the derived current milestone.
- `status=partial` and `status=blocked` record the scene but do not advance the cursor.
- The current milestone is derived as the first plan-derived milestone without a completed checkpoint.

Pal must not infer work order progress from chat text, progress text, or terminal summaries. The fact source is the minion tasking store.

## Workspace And Artifacts

Minion work happens inside a task environment. There are two workspace kinds:

- `git_repo`: used by coder-style profiles that produce code changes.
- `folder`: used by planner, reviewer, generic, and other one-shot profiles that only need a private output folder.

All prepared workspaces expose:

- `workspace_kind`
- `run_dir`
- `artifact_dir`

Read-only profiles may still receive `repo_path` for source inspection, but their deliverables are written only under `artifact_dir`. This keeps planner/reviewer output from polluting the source repo.

Coder work remains Git-backed:

- A task owns or resolves a task repo.
- A work order runs on its own branch in that repo.
- A milestone completion is represented by one Git commit on the work order branch.
- A completed checkpoint records the milestone index and the corresponding commit SHA.
- Coder reports and test notes are written under `minion_outputs/{work_order_id}/` inside the task repo and are included in the commit when relevant.

This keeps the minion ledger and workspace state aligned: the minion tasking store knows which milestone is complete, and Git can restore the files for that milestone.

Spawn must read or initialize the task repo before starting a runner. For an existing work order, spawn must checkout or create the work order branch from the recorded base ref. A runner must not do task work on Pal's live branch unless that workspace was explicitly assigned as the task repo.

Non-coder profiles get an isolated folder workspace:

- `data/minion/workspaces/{run_id}_{profile}/work_order.json`
- `data/minion/workspaces/{run_id}_{profile}/metadata.json`
- `data/minion/workspaces/{run_id}_{profile}/logs/`
- `data/minion/workspaces/{run_id}_{profile}/deliverables/`

The runner exposes `op_minion_artifact_write` so minions can write structured deliverables under `artifact_dir`. The terminal and checkpoint payloads include `artifacts[]` and `primary_artifact`.

When a Git-backed runner finishes a milestone:

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
- `intro_minion_plan_search`
- `intro_minion_plan_read`
- `op_minion_draft_work_order`
- `op_minion_promote_work_order_draft`
- `op_minion_submit_plan`
- `op_minion_accept_plan`
- `op_minion_revise_plan`
- `op_minion_spawn`
- `op_minion_kill`
- `op_minion_finalize`

`intro_work_order_read` returns work order status, milestones, derived current milestone, latest checkpoint, latest completed checkpoint, recent ledger, current worker, task lessons, and pending system lesson candidates.

`current_worker` comes from manager active runs bound to the work order. It is not inferred from conversation context.

`op_minion_spawn` may accept `task_query` instead of `work_order_id`. The minion repository resolves it through work order search only when there is exactly one candidate. Multiple candidates or no candidates return facts for Pal to show or ask about; Pal must not guess.

`op_minion_spawn` may also accept `draft_id` for the existing draft promotion path. Draft milestones are review input and should be converted into an accepted plan before new execution work. This path is not a replacement for plan-first dispatch.

For new planned work, Pal must not hand-write spawn milestones. If no planner minion has produced a plan, Pal acts as a fallback planner by writing a small `FinalPlanArtifact` through `op_minion_submit_plan`. That operation writes an immutable plan file and returns a `plan_ref`. The plan file is the only milestone truth source, including for generic, reviewer, lifestyle, and other non-coder profiles. `op_minion_spawn` consumes an accepted top-level `plan_ref`; auxiliary files, review reports, checkpoints, and research outputs go in `supporting_artifacts` and never drive execution.

`op_minion_spawn` does not accept public `artifact_refs`, `minion_profile`, `allowed_capabilities`, `resolved_profile`, `milestones`, inline `plan_artifact`, `TaskContextPack`, `module_execution`, or `prompt_view`. Public profile selection uses `profile_group` plus `profile_name`; the manager resolves the runtime profile snapshot and allowed capabilities. Raw work-order milestones without a plan are ignored for execution and must not create serial milestone state. Missing dispatch source, unknown work order id, unaccepted plan ref, bad plan schema, or invalid topology is an invalid/blocking state, not a fallback opportunity.

Plan refs are not dispatchable just because they are valid JSON. A plan must pass review and then be accepted through explicit control before implementation spawn can consume it. Review pass opens a plan acceptance interaction with Accept, Reject, and Edit actions. Accept writes a separate acceptance marker. Reject and Edit record decision state and require an explicit revision path. Revisions use `op_minion_revise_plan`, which writes a new immutable plan revision instead of mutating the old file.

`op_minion_spawn` may accept optional `preferred_endpoint_id`. Pal should set it only when the user explicitly asks for a specific model or LLM endpoint for that minion, after resolving the request to an enabled endpoint id. If omitted, the runner follows the normal Pal active endpoint setting from the runtime database.

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

Current builtin profiles are `generic`, `planner`, `coder`, and `reviewer`. Public calls identify them as `profile_group` plus `profile_name`, such as `software_engineering` / `coder`; canonical internal ids such as `software_engineering.coder` remain runtime metadata. `planner` owns both planning and architecture review: work-order decomposition, module/interface boundaries, design tradeoffs, risks, migration notes, and verification steps.

Profile resolution order is builtin templates, runtime profile files, then currently mounted provider declarations. Later declarations with the same `profile_id` override earlier ones.

Profiles may declare capability groups. `core_minion_read` includes scoped discovery/read/call plus read-only `op_memory_recall`, so runners can recall relevant experience without memory-write access. `web_research` expands to `op_web_search` and `op_web_read`, so research-capable minions reuse Pal's existing web tools instead of owning a second web integration.

Profiles may also use `capability_policy.mode = "inherit_filtered"`. In that mode, spawn starts from the manager-visible capability surface, adds profile defaults and provider hook results, then applies the minion deny policy. For `profile_only`, the profile is the upper bound; public spawn cannot expand it with ad hoc `allowed_capabilities`.

Profiles may declare `gate_policy` and `output_policy`. `gate_policy.after_each_milestone = ["reviewer_gate"]` makes the manager insert a reviewer gate after each coder milestone. Reviewer requirements such as command/test evidence, API evidence, LSP evidence, and public declaration implementation checks are profile policy, not LLM prompt folklore. LSP stays a reviewer evidence tool rather than a separate manager gate.

The runner consumes only the resolved profile snapshot in `TaskContextPack`. It does not query PalCore for profile policy after spawn.

## Runner Capability Boundary

The runner is allowed to execute task capabilities, report milestone events, and request approval. It must not manage Pal or the minion subsystem.

Profile resolution applies a default deny policy before the runner starts. The runner also rechecks the same policy before exposing tools or executing tool calls.

Denied by default:

- all `intro_*` capabilities
- all `op_minion_*` capabilities
- memory writes such as `op_memory_write` and `op_memory_update`
- behavior, skill, channel, plugin, lifecycle, attach/detach/rescan/enable/disable style operations

This prevents recursive spawn/kill/list/read behavior. A task runner does not need to know the minion control plane exists; Pal observes and manages that layer through the minion module capabilities.

`op_minion_artifact_write` is the one internal exception to the `op_minion_*` deny rule. It is scoped to `workspace.artifact_dir` and cannot control the minion subsystem.

## Runner Loop

The runner is a thin execution entity, not a forked Pal. It starts a slim runtime with LLM, execution, artifact metadata, read-only memory recall, and allowed task tools such as file read/edit/write, shell/code execution, and web search/fetch. It does not load channel endpoints, proactive triggers, control panel, or Pal user-facing routing.

If `TaskContextPack.metadata.preferred_endpoint_id` is present, the runner forwards it as `CanonicalLLMRequest.metadata.preferred_endpoint_id` and uses that endpoint's budget when resolving max output tokens. Without that metadata, no preferred endpoint is passed; the slim runtime reads the current active LLM endpoint from `pal.sqlite3`.

`TaskContextPack.allowed_capabilities` is the internal allowed pool. To keep token cost low, the normal LLM tool surface exposes only a small resident work set: `op_tool_search`, `op_tool_read`, `op_tool_call`, `op_file_read`, `op_file_edit`, `op_file_write`, `op_exec_shell`, `op_minion_artifact_write`, `op_web_search`, `op_web_read`, and `op_memory_recall` when those capabilities are allowed. Discovery runs through a scoped execution view, so denied or non-allowed capabilities cannot appear in search/read results.

When `op_minion_artifact_write` is available, the runner prompt asks the minion to write the primary deliverable to `artifact_dir` and keep the final summary short. If a text-deliverable run finishes with text but no explicit artifact, the runner writes an automatic `milestone_{index}_{profile}.md` deliverable.

Tool calls are executed through the existing `ExecutionRuntime` path and must be present in `TaskContextPack.allowed_capabilities`.

If a tool or capability call fails and read-only `op_memory_recall` is allowed, the runner prompt requires recall of relevant prior experience before retrying, debugging further, or reporting the milestone blocked.

High-risk calls declared by the task approval policy pause the runner and emit `approval_requested`. Reject or edit decisions block the milestone; accept continues the tool call.

## Lessons

Terminal minion events may carry:

- `task_lessons`
- `system_lessons`

The runner may write lesson sections in its terminal summary, but the terminal payload extracts them into structured fields and strips them from the user-facing completion text.

Lessons are never absorbed silently. If a terminal event carries lessons, Pal opens a `minion_lesson_approval` interaction with:

- `Accept`
- `Reject`
- `Edit`

Accept stores task lessons as tasking continuity and stores system lessons as accepted system candidates. It also attempts to commit accepted lessons to L3 memory when `op_memory_write` is available in the current runtime. Reject discards the proposed lessons. Edit pauses absorption and asks for revised lesson text before saving.

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

1. Pal captures user brainstorming with `minion_draft_work_order`.
2. Architect reviews the draft as a bounded input and builds a structured plan with plan builder tools, not hand-written JSON.
3. `plan_validate_and_submit_for_review` freezes a primary draft snapshot for the `plan_acceptance` gate and writes a mechanical `plan_review.md` summary artifact next to the machine `plan.draft.json`.
4. Reviewer reads the plan through `plan_read`/`plan_find`/`plan_get` handles, submits a gate verdict, and cites target handles for required fixes.
5. Pal/user accepts the reviewed `plan_ref`, or requests/rejects a revision through the plan control interaction.
6. The manager ticks the accepted workflow DAG and dispatches downstream coder/join modules according to the plan DAG and profile policy.

Architect must not invent task boundaries from raw chat history when a draft exists. The draft is the bounded source for review.

Architect plan construction is structured and mutable during drafting:

- `plan_begin` creates a draft handle.
- Module and milestone tools add bounded structure. `module_key` is caller-chosen, stable, and human-readable; generated handles are internal mutation references.
- `plan_add_module_outline` and `plan_add_milestone_outline` close their nodes in one call. `begin_*`/`end_*` tools are for incremental construction and return parent handles so the planner can continue at the correct layer.
- Revision planners use `plan_checkout`, `plan_find`, `plan_get`, and `plan_update_*`/`plan_delete_*` tools to repair specific handles instead of rebuilding the whole plan from scratch.
- The runtime validates topology, closed nodes, acceptance criteria, module dependencies, and gate evidence fields before a draft can be submitted for review.
- Domain plugins may expose plan builder aliases for their own vocabulary, but aliases must deterministically map back to the core plan builder operations. The compiled artifact remains the canonical DAG; alias output is not a second plan format.

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
- `git_worktree`: used by coder-style profiles when the source repository is a local Git repository and the task can share the same object store through `git worktree`.
- `folder`: used by architect, reviewer, generic, and other one-shot profiles that only need a private output folder.

All prepared workspaces expose:

- `workspace_kind`
- `run_dir`
- `artifact_dir`

Read-only profiles may still receive `repo_path` for source inspection, but their deliverables are written only under `artifact_dir`. This keeps planner/reviewer output from polluting the source repo.

Coder work remains Git-backed:

- A task owns or resolves a project repo/workspace. `task_id` remains the tasking identity; `project_name` names the long-lived repository/workspace scope.
- A work order runs on its own branch in that project repo.
- A milestone completion is represented by one Git commit on the work order branch.
- A completed checkpoint records the milestone index and the corresponding commit SHA.
- Coder reports and test notes are written under `minion_outputs/{work_order_id}/` inside the project repo/workspace and are included in the commit when relevant.

This keeps the minion ledger and workspace state aligned: the minion tasking store knows which milestone is complete, and Git can restore the files for that milestone.

Spawn must read or initialize the project repo before starting a runner. For an existing work order, spawn must checkout or create the work order branch from the recorded base ref. A runner must not do task work on Pal's live branch unless that workspace was explicitly assigned as the project repo.

The default prepared repo layout is `data/minion/repos/{parent_work_order_id}/{project_name}/{module_name}` for module work, with explicit `task_repo_path` or `target_repo_path` still taking precedence. `project_name` is read from metadata/workspace when present, otherwise it falls back to the source repo name and then `task_id`. `module_name` is the human-readable module folder name and remains compatible with existing `module_id` plan artifacts.

Plan-parent module execution keeps module branches isolated while preserving declared dependency flow. After a module checkpoint passes its gate and the module completes, the parent records that child repo/branch as the next serial dependency baseline. The next module child still gets its own module workspace and work-order branch, but local Git baselines are materialized as `git worktree` checkouts instead of full clones; remote or nonlocal baselines fall back to clone. This makes prelude/contracts and accepted upstream public interfaces visible without copying the same repository repeatedly or allowing arbitrary sibling internals.

While a work order is running, each module worktree contains a full checkout of the current baseline. Seeing the same canonical path such as `src/package/contracts.py` under multiple module folders is expected worktree behavior, not a copied contract. After the parent work order completes, Pal cleans the execution scene by keeping the final/join worktree, common `.git` store, and `_artifacts`, then removing intermediate module and temporary integration worktrees with `git worktree remove --force`. Branches and commits remain in the common Git store for audit/recovery.

Module boundaries are contract boundaries. Planner output must describe cross-module handoffs through `provided_interfaces`, `consumed_interfaces`, `cross_module_contracts`, and prelude/contracts stubs or public facades when downstream modules need importable types or APIs. Coder and reviewer profiles treat undeclared cross-module imports as contract violations, including in the join module. Join may compose modules only through declared public interfaces, exported facades, or prelude contracts.

Plan-parent module scheduling is DAG-first. The accepted plan artifact remains the truth source. The repository persists the serialized runtime state in `plan_execution.dag_state` plus `dag_revision`; storage uses canonical `node_*` fields while runtime views may still render nodes as modules for software-engineering profiles. `dag_advancer` owns the typed mechanical model (`DagSpec`, `DagState`, and `DagAdvanceResult`) for graph construction, indegree transitions, ready/running/completed/stale state, replay merge, and stale-child rejection. The manager does not own DAG truth: it supplies strategy and resources, including auto-advance policy, concurrency limit selection, profile selection, slot accounting, IPC, and user notification.

The scheduler derives runtime state from minion-owned facts, not from the projection file: completed modules come from parent/module checkpoints, running modules come from active child work orders, and blocked/failed modules come from terminal ledger state. On each scheduling signal, the manager recomputes the ready set from the DAG:

- a module is ready when it is not completed, not running, not blocked, and all dependency modules are completed
- ready modules enter a waiting queue ordered by the validated topology order
- the global `max_parallel_modules` limit controls how many ready modules may start
- `max_parallel_modules=5` is the default global module concurrency limit
- `max_parallel_modules=1` gives serial behavior without a manual module-boundary DAG tick
- larger values allow independent ready modules to run concurrently in isolated worktrees

Module completion, parent spawn, manager startup recovery, retry, and explicit DAG tick are scheduling signals. `tick_parent_dag` is a manual recovery/control path, not the normal module-boundary driver. The normal path is: gate passes a child module checkpoint, parent records the module completion, the scheduler recomputes ready modules, and the manager starts the next available child module when a global concurrency slot is free. On manager startup, stale `running_module` children without active runner processes are released back to the DAG as ready work, then ready parents are automatically scheduled after the manager socket is listening. Set `PAL_MINION_AUTO_RESUME_READY_MODULES=0` to keep startup recovery ledger-only and require an explicit DAG tick. When the DAG completes, the parent writes a mechanical `completion_report.md` artifact under the work-order artifact directory; user-facing notifications should point to that artifact rather than paste the full report.

Concurrency is intentionally global at the minion scheduler layer. Per-endpoint request limits belong to the LLM broker or endpoint invoker because endpoint fallback can change the actual provider/model used by a child run. The parent module scheduler should not pre-resolve endpoint identity or duplicate broker fallback policy.

## Runner Sandbox

Minion runners may run inside a task sandbox. Sandbox setup is part of manager spawn preparation and is recorded in `TaskContextPack.metadata.sandbox`.

Current behavior:

- Linux uses `bubblewrap` (`bwrap`) when available.
- Unsupported hosts or unwired backends record `backend="unavailable"` with a reason instead of pretending isolation exists.
- Operators can disable sandboxing with `metadata.sandbox.enabled=false` or `PAL_MINION_SANDBOX=0`.
- Network remains open so research-capable minions can use web tools and package managers when the task permits.
- LLM credentials are not passed into the sandbox. Secret-like environment variables are scrubbed, and LLM calls go through the host minion LLM broker (`PAL_MINION_LLM_BROKER=1`).
- The sandbox gives each run private `HOME`, `TMPDIR`, cache, and pycache paths under `runtime_root/data/minion/sandbox/runs/{run_id}`.
- Pal source, Python dependency paths, config, plugin data, skills, and selected runtime data are mounted read-only unless the runner needs task-owned state.
- The assigned repo/workspace is mounted writable only for coder-style workspaces that own that path.
- A deny-bin projection replaces high-risk host commands such as `sudo`, `systemctl`, `docker`, `ssh`, and namespace/mount tools with wrappers that tell the runner to use resident Pal tools.

The sandbox is a runtime boundary, not a replacement for profile policy. The runner still receives a scoped capability surface, minion deny rules still apply, and file/git tools still enforce workspace-relative behavior. The main goal is to reduce host mutation risk and tool-routing friction while keeping minions useful.

Non-coder profiles get an isolated folder workspace:

- `data/minion/workspaces/{run_id}_{profile}/work_order.json`
- `data/minion/workspaces/{run_id}_{profile}/metadata.json`
- `data/minion/workspaces/{run_id}_{profile}/logs/`
- `data/minion/workspaces/{run_id}_{profile}/deliverables/`

The runner exposes scoped deliverable tools so minions can write structured files under `artifact_dir`. General report-style profiles use `artifact_write`; software architect profiles use plan builder tools that compile and register the primary `plan.json` artifact. The terminal and checkpoint payloads include `artifacts[]` and `primary_artifact`.

When a Git-backed runner finishes a milestone:

1. Stage the milestone's task-scoped changes.
2. Commit them to the work order branch.
3. Emit `checkpoint(status=completed, milestone_index=..., commit_sha=...)`.

If there are no file changes for a milestone, the checkpoint may record `git_commit.status = "no_changes"` and still advance the milestone when the milestone's acceptance criteria are satisfied.

If commit fails, the runner must emit `partial` or `blocked`; it must not emit a completed checkpoint for that milestone.

After user acceptance, the minion subsystem may squash the milestone commits into one final work-order commit and merge or apply it to the configured target branch. This finalization is a separate control operation from milestone checkpointing.

## Capabilities

The minion module exposes:

- `task_search`
- `task_read`
- `work_order_search`
- `work_order_read`
- `work_order_draft_search`
- `work_order_draft_read`
- `minion_list`
- `minion_read`
- `minion_profile_list`
- `minion_profile_read`
- `minion_plan_search`
- `minion_plan_read`
- `minion_attach`
- `minion_detach`
- `minion_configure`
- `minion_dispatch_workflow`
- `minion_review_gate_submit`
- `minion_draft_work_order`
- `minion_promote_work_order_draft`
- `minion_kill`
- `minion_finalize`
- `minion_tick_parent_dag`
- `minion_submit_repair_bill`
- `minion_pause_work_order`
- `minion_recover_work_order`
- `minion_destroy_work_order_run`

`work_order_read` returns work order status, milestones, derived current milestone, latest checkpoint, latest completed checkpoint, recent ledger, current worker, module status projections (`module_status_list` and `module_status_text`) for plan-parent DAGs, task lessons, and pending system lesson candidates.

`current_worker` comes from manager active runs bound to the work order. It is not inferred from conversation context.

`minion_dispatch_workflow` is the normal public delegation entrypoint. Pal passes `task_id`, the prepared goal, optional `requirements_brief`, workspace facts, `architecture_mode`, `interaction_mode`, and optional `preferred_endpoint_id`. The task binds `profile_family`; dispatch does not take profile selectors. The manager creates the work order, runs the family DAG producer or generic single-node DAG producer, then consumes the resulting DAG mechanically instead of exposing low-level runner mechanics.

Accepted or mechanically generated plan artifacts are consumed as DAGs. Module metadata or topology nodes may declare `executor_profile` for a node that needs a different executor from the DAG default. The manager records `plan_execution.dag_execution.default_executor_profile` and `node_executors`, then schedules ready nodes mechanically from that table.

`minion_draft_work_order` and `minion_promote_work_order_draft` remain auxiliary paths for user brainstorming and draft reuse. Draft milestones are review input only; they should be promoted or folded into a normal workflow before execution work starts.

Plan refs are not dispatchable just because they are valid JSON. A plan must pass review and then be accepted through explicit control before implementation dispatch can consume it. Review pass opens a plan acceptance interaction with Accept, Reject, and Edit actions. Accept writes a separate acceptance marker. Reject and Edit record decision state and let the manager dispatch a revision architect, which writes a new immutable plan revision instead of mutating the old file.

`minion_dispatch_workflow` may accept optional `preferred_endpoint_id`. Pal should set it only when the user explicitly asks for a specific model or LLM endpoint for that minion, after resolving the request to an enabled endpoint id. If omitted, the runner follows the normal Pal active endpoint setting from the runtime database.

Natural-language minion control should resolve facts first. For requests like "what is it doing?", "replace it", "tick this DAG", or "merge the completed work", Pal should inspect active runs and work order snapshots, then call the relevant operation. Pal must not infer current worker, progress, or milestone completion from conversation text.

## Runner Continuity

The manager resolves the requested profile, applies role defaults and capability exposure hooks, then asks the minion repository to prepare continuity.

`TaskContextPack.continuity` includes:

- current milestone
- completed milestones
- latest checkpoint
- latest completed checkpoint
- recent ledger
- task-wise lessons

The manager repeats this preparation before launching the runner, so reattach/retry uses live minion-owned facts.

## Compact And Resume Context

Minion compact is a task-state recovery packet, not a Pal conversation summary and not a memory-writing path.

Runtime uses a dedicated minion compact schema:

- `pal.compaction.minion.v1`

The minion compact prompt and renderer are separate from Pal compact. The minion prompt does not ask for user preference tracking, collaboration history, or `memory_candidates`. The rendered context is:

- `<compact_context kind="minion" authority="reference_only">`
- title: `Minion Task Continuity Reference`
- task-oriented fields such as `task_goal`, `current_milestone_hint`, `claimed_completed`, `claimed_pending`, `implementation_decisions`, `verification_hints`, `review_or_repair_hints`, `must_verify_against`, and `next_action_hint`

The compact output is explicitly reference-only. A runner must verify against the work order, accepted plan artifact, current milestone, checkpoint/ledger, Git state, and current files before claiming progress or completion. Compact text may say that work is "claimed completed", but it is not a truth source.

Minion compact must not create durable memory candidates. Reusable lessons or case memories are proposed at work-order terminal/finalization time and then routed through Pal approval.

## Profiles

Minion profiles are declarative. Builtin profiles are package TOML templates, and runtime profiles are loaded from `runtime_root/plugins/minion/profiles/*.toml`.

Current builtin profiles are `generic`, `architect`, `coder`, `reviewer`, `review_worker`, and `writer`. Public calls identify them as `profile_group` plus `profile_name`, such as `software_engineering` / `coder`; canonical internal ids such as `software_engineering.coder` remain runtime metadata. Pal's main agent owns user-facing requirements shaping, while `architect` owns implementation architecture: work-order decomposition, module/interface boundaries, design tradeoffs, risks, migration notes, and verification steps. `reviewer` is a gate strategy profile for plan/checkpoint/repair verdicts. `review_worker` is a terminal standalone review-artifact executor that can be selected by an architect plan for review-only workflows. `writer` is for bounded technical writing and evidence-based documentation artifacts; it is not a code implementation profile.

Profile resolution order is builtin templates, runtime profile files, then currently mounted provider declarations. Later declarations with the same `profile_id` override earlier ones. Runtime files seeded from builtin templates carry `[metadata] builtin = true`; provisioning refreshes those managed seed copies on upgrade after writing a timestamped backup under `plugins/minion/profile_backups/`. Custom runtime profiles should omit that builtin marker or use a distinct profile id.

Profiles may declare capability groups. `core_minion_read` includes scoped discovery/read/call plus read-only `memory_recall`, so runners can recall relevant experience without memory-write access. `web_research` expands to `web_search` and `web_read`, so research-capable minions reuse Pal's existing web tools instead of owning a second web integration.

Profiles may also use `capability_policy.mode = "inherit_filtered"`. In that mode, workflow dispatch starts from the manager-visible capability surface, adds profile defaults and provider hook results, then applies the minion deny policy. For `profile_only`, the profile is the upper bound; public calls cannot expand it with ad hoc `allowed_capabilities`.

Profiles may declare `gate_policy` and `output_policy`. Gate policy names gate definitions instead of expanding every reviewer/checklist detail into each profile. For example, coder uses `gates = ["checkpoint_admission", "module_quality"]`, architect uses `gates = ["plan_acceptance"]`, and generic uses `gates = ["none"]`. Output artifacts may declare `metadata.workflow_next` to override the profile default next step; profile `output_policy.workflow_next` remains the allowed-next policy and fallback. Plan modules may declare `metadata.executor_profile` only as a per-DAG-node override when one node is consumed by a different profile from the artifact default; group-local names are resolved against the default executor profile group. Runtime gate definitions expand gate names into target kind, gate kind, execution strategy, reviewer profile, repair/revision bounds, required checks, and blocking classes. Milestone result events are the v1 gate trigger; profile TOML should not spell out trigger mechanics.

The pre-plan `source_contract` compiler is an opt-in contract-locking layer for unusually risky or strongly constrained planning tasks. It is not part of the default architecture path. Callers must explicitly request it through work-order metadata, such as `enable_pre_plan_contract=true`; otherwise the architect writes the structured plan directly and the `plan_acceptance` gate reviews the resulting artifact.

Gate definitions and individual gate checklist entries are extension points. New minion plugins may declare additional gate definitions or checklist entries; profiles reference the gate name, while the definition owns the concrete checks and default strategy. The active gate result is projected into `active_gate_todo` so reviewer failures, repair checklist items, and milestone todo state use the same ledger shape. Reviewer requirements such as command/test evidence, API evidence, LSP evidence, and public declaration implementation checks belong in gate definitions/policy, not LLM prompt folklore. LSP stays a reviewer evidence tool rather than a separate manager gate.

The runner consumes only the resolved profile snapshot in `TaskContextPack`. It does not query PalCore for profile policy after launch.

## Gate Loop

The manager applies profile gate policy before launching a runner. `gate_specs_from_pack(...)` expands the resolved profile's `[gate_policy] gates = [...]` names into concrete `GateSpec` values and stores them in work-order metadata as `gate_specs`. `gates = ["none"]` expands to no gate. The v1 trigger is `after_each_milestone`; profiles should name gates, not duplicate trigger mechanics or reviewer fields.

Every milestone result event goes through `ReviewOrchestrator.schedule_event_gates(...)`. The orchestrator iterates the active `GateSpec` values for the trigger and uses the gate strategy registry to decide whether a strategy is runnable. Builtin `reviewer` strategy gates currently route by `target_kind`:

- `plan_artifact` -> plan review/acceptance loop
- `checkpoint` -> checkpoint verification/repair loop

This is profile-driven orchestration, not a planner-vs-coder special case in the manager loop. A new profile gets a different gate by referencing a different gate name; a new execution mechanism should register a `GateStrategy` rather than adding ad hoc manager branches.

### Plan Acceptance Gate

`plan_acceptance` applies to architect-style milestones. An architect milestone must emit `status=completed`, a `plan_ref`, and valid plan validation data before the gate schedules. The manager spawns a reviewer work order with a read-only review workspace and a `review_target` containing the exact plan ref, validation result, source work order, and gate spec.

The reviewer must submit the verdict with `op_minion_review_gate_submit` and `gate_kind=plan_acceptance`.

Manager reconciliation handles verdicts as follows:

- `pass`: update plan review state to acceptance pending and emit `plan_acceptance_pending`. Implementation dispatch still requires plan acceptance through the control interaction or an explicit human override marker; a valid JSON plan plus reviewer pass is not enough by itself.
- `fail`: spawn an automatic revision architect when the gate policy allows it and revision attempts remain; otherwise record `plan_revision_required`.
- `partial`: record `plan_review_human_decision_required`.

Plan revisions write new immutable plan revisions. They must not mutate the original plan artifact.

### Checkpoint Verification Gate

Coder checkpoint gating is split by cost and semantic depth. `checkpoint_admission` applies to ordinary implementation checkpoints and is a cheap admission/router gate: the checkpoint must have a structured commit/report, changed-file evidence, relevant command/test evidence, owned-area/workspace-policy compliance, and clean LSP/type/build diagnostics when available or a concrete unavailable/not-applicable reason. It does not claim semantic module acceptance and does not spawn a reviewer by default.

`module_quality` applies at the module boundary. The runner emits the terminal module checkpoint as `status=claimed`, not `completed`, and the coder runner waits for a manager control message before downstream modules may depend on the module. The reviewer checks semantic contract fit, public API/type/schema guarantees, module and cross-module handoffs, corner cases, lifecycle/ownership, delivery-surface dogfood, and downstream readiness. Legacy `checkpoint_quality` remains available for callers that explicitly want full reviewer gates on every checkpoint.

Planner builder output should mirror that split. Module outlines may carry `module_quality_criteria`, `risk_surfaces`, and `delivery_surfaces`; milestone outlines may carry `checkpoint_admission_evidence`. These fields are language-neutral and describe what the gate must prove, not how a specific language's test tool works.

The manager spawns a reviewer work order for the claimed checkpoint. The review target includes the checkpoint id, work order id, run id, module/milestone ids, acceptance criteria, source contract, commit SHA, checkpoint git context, and gate spec. Checkpoint and repair reviewers should submit through `op_minion_review_checkpoint`; the generic `op_minion_review_gate_submit` surface is hidden for these targets.

Checkpoint review includes delivery-surface challenge, not only source and unit-test review. A delivery surface is any user/downstream-facing invocation or integration boundary: CLI binary/script, package entrypoint, public library/header/API consumer path, generated wrapper, service/unit launch, HTTP/UI route, plugin/provider hook, command wrapper, or persisted/import/export format. The reviewer should verify the actual downstream invocation when practical, or run the smallest faithful wrapper/link/import/launch/roundtrip probe that preserves that contract. Internal helper/function tests do not prove a wrapper, manifest, framework adapter, public consumer path, or persisted-format contract works. For machine-readable CLI contracts, parse-layer/framework errors are part of the output contract when the source says output is JSON or machine-readable.

Manager reconciliation handles checkpoint verdicts as follows:

- `pass`: `close_checkpoint_from_review_gate(...)` records milestone closure from the pass gate. In serial/module execution, the manager sends the next milestone turn to the same coder runner when possible; otherwise it sends a `complete` control message.
- `fail`: send a bounded `repair_turn` to the same coder runner when runner control is available.
- `partial`: send repair when the partial gate carries required fixes, blocker/high-severity findings, or material contract impact; otherwise block and surface the partial review.

Repair turns carry `active_gate_todo`, `checkpoint_repair`, and structured reviewer feedback. The coder must treat the repair checklist as the complete scope, repair the same milestone, and produce a new checkpoint claim. Reusing the failed checkpoint commit as the repair checkpoint is blocked. Automatic repair attempts are bounded by the gate definition; builtin `module_quality` and legacy `checkpoint_quality` default to 5 attempts.

### Gate Ledger And Evidence

`submit_review_gate(...)` stores every gate decision in `minion_review_gates`, records ledger events, validates target binding, and projects failed/partial gate findings into `active_gate_todo` on the source work order. The same projection feeds reviewer repair context, active todo state, and future status inspection.

Pass gates that cite command, API, source, or LSP evidence must be backed by recorded tool evidence whenever the runner can validate provenance. Reviewers should prefer semantic `evidence_selectors` such as command substrings, source path substrings, or LSP operations; runtime resolves them to recorded tool evidence and fills command, exit status, output summary, and LSP details. `EV-*`, `call_id`, and `ledger_id` are runtime identifiers, not fields reviewers should invent by hand.

## Runner Capability Boundary

The runner is allowed to execute task capabilities, report milestone events, and request approval. It must not manage Pal or the minion subsystem.

Profile resolution applies a default deny policy before the runner starts. The runner also rechecks the same policy before exposing tools or executing tool calls.

Denied by default:

- all `intro_*` capabilities
- all `op_minion_*` capabilities
- memory writes such as `memory_write` and `memory_update`
- behavior, skill, channel, plugin, lifecycle, attach/detach/rescan/enable/disable style operations

This prevents recursive spawn/kill/list/read behavior. A task runner does not need to know the minion control plane exists; Pal observes and manages that layer through the minion module capabilities.

`artifact_write` and planner `plan_*` builder tools are internal exceptions to the `op_minion_*` deny rule. `artifact_write` is scoped to `workspace.artifact_dir`; planner builder tools only maintain a planner-local draft and compile it into the normal primary `plan.json` artifact. Neither path can control the minion subsystem.

## Runner Loop

The runner is a thin execution entity, not a forked Pal. It starts a slim runtime with LLM, execution, artifact metadata, read-only memory recall, and allowed task tools such as file read/edit/write, shell/code execution, and web search/fetch. It does not load channel endpoints, proactive triggers, control panel, or Pal user-facing routing.

If `TaskContextPack.metadata.preferred_endpoint_id` is present, the runner forwards it as `CanonicalLLMRequest.metadata.preferred_endpoint_id` and uses that endpoint's budget when resolving max output tokens. Without that metadata, no preferred endpoint is passed; the slim runtime reads the current active LLM endpoint from `pal.sqlite3`.

`TaskContextPack.allowed_capabilities` is the internal allowed pool. To keep token cost low, the normal LLM tool surface exposes only a small resident work set: `tool_search`, `tool_read`, `tool_call`, `file_read`, `file_edit`, `file_write`, `delete_path`, `git`, `shell`, `tree`, `search`, `artifact_write`/`artifact_edit`, architect `plan_*` builder tools, `web_search`, `web_read`, `memory_recall`, and LSP/code-intelligence tools when those capabilities are allowed. Discovery runs through a scoped execution view, so denied or non-allowed capabilities cannot appear in search/read results.

When `artifact_write` is available, the runner prompt asks the minion to write the primary deliverable to `artifact_dir` and keep the final summary short. Software architect profiles instead use the plan builder tools, starting with `plan_begin`, adding module and milestone outlines plus acceptance criteria, then finishing with `plan_validate_and_submit_for_review`; that call validates the draft and submits the primary plan artifact for the existing `plan_acceptance` gate. If a text-deliverable run finishes with text but no explicit artifact, the runner writes an automatic `milestone_{index}_{profile}.md` deliverable.

Tool calls are executed through the existing `ExecutionRuntime` path and must be present in `TaskContextPack.allowed_capabilities`.

If a tool or capability call fails and read-only `memory_recall` is allowed, the runner prompt requires recall of relevant prior experience before retrying, debugging further, or reporting the milestone blocked.

High-risk calls declared by the task approval policy pause the runner and emit `approval_requested`. Reject or edit decisions block the milestone; accept continues the tool call.

## Lessons

Terminal minion events may carry:

- `task_lessons`
- `system_lessons`
- `memory_candidates`

The runner may write lesson sections in its terminal summary, but the terminal payload extracts them into structured fields and strips them from the user-facing completion text.

Lessons are never absorbed silently. If a terminal event carries lessons, Pal opens a `minion_lesson_approval` interaction with:

- `Accept`
- `Reject`
- `Edit`

Accept stores task lessons as tasking continuity and stores system lessons as accepted system candidates. It also attempts to commit accepted lessons to L3 memory when `memory_write` is available in the current runtime. Reject discards the proposed lessons. Edit pauses absorption and asks for revised lesson text before saving.

Pure `memory_candidates` from a terminal event use the generic `memory_candidate_approval` interaction instead of the minion lesson interaction. Accept routes through Pal's `memory_candidate_decision` handler and writes approved candidates through `op_memory_write` when available. This keeps tasking lessons, system lesson absorption, and durable case/fact memory candidates separate.

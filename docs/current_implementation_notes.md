# Current Implementation Notes

This file is a short sync point for the current codebase when older design notes lag behind implementation.

## Prompt Assembly

Current system block order:

```text
Identity -> System Surfaces -> Operating Rules -> Behavior Routing -> Memory Routing -> Skill Learning -> Resident Affordances -> Memory Context -> Runtime Overlay
```

Ownership:

- Identity comes from the identity provider.
- System Surfaces and Operating Rules come from core prompt fragments.
- Behavior Routing and Resident Affordances come from behavior providers.
- Memory Routing and memory context projection come from memory providers.
- Skill Learning comes from skill providers.

`System Surfaces` is a real top-level system section. It is not embedded inside Operating Rules.

## Memory Projection

The current memory prompt no longer exposes one generic `Working Memory` block.

Current memory-context blocks:

- `Recent Context`
- `Current Summary`
- `Remembered Facts`
- `Relevant Experience`
- `Active Route Guidance`

If memory has been recalled or is present in the prompt, Pal is instructed to treat it as reference context before decisions or actions. This is a prompt-level governance rule; runtime still cannot inspect the model's private reasoning.

## Artifacts

Current-turn artifacts are driven by `event.payload["artifact_refs"]`.

Rules:

- Explicit current artifact refs are authoritative.
- Empty current text with no current artifact refs does not expose historical hot artifacts.
- Historical hot artifact fallback requires explicit artifact/file/image/audio language.
- Weak deictic references such as "this" or "that" do not trigger historical hot artifact exposure.
- URL text is ignored when checking historical artifact relevance.
- LLM-visible artifact metadata hides source secrets.
- `local_file.preferred_path` may be shown only as safe metadata for tools that explicitly accept local paths.

## Tool Surface

The direct LLM tool surface is configured by `src/pal/core/tool_surface.toml`.

Resident artifact tools are intentionally limited to:

- `op_artifact_info`
- `op_artifact_read`

Other artifact tools remain discoverable.

MCP-projected tools and prompt render capabilities are not resident by default.

## Control And Channel

Slash commands are runtime-private control ingress. They do not enter the LLM prompt, do not become conversational input, and do not write the raw command text into L1. The control plane parses them deterministically into `ControlAction` values.

Telegram command text may arrive with a bot mention suffix, such as `/control@PalDevBot`. The command normalizer strips the leading slash and the optional `@BotName` suffix before lookup, so `/control`, `/control@PalDevBot`, and `/refresh_llm_endpoint@PalDevBot` resolve to the same registered commands.

`/refresh_llm_endpoint` is a built-in control command. It bypasses LLM reasoning and asks PalCore to refresh LLM endpoint topology from the local database for future turns. It is exposed in:

- the `/control` textual command list
- the Telegram command catalog
- the inline control panel as the `Refresh LLM` button

The inline button uses the generic `control.command.run` interaction action, which dispatches the same command handler as the slash command. Refreshing LLM endpoints changes routing for future turns only; it is not a mid-turn model switch.

Channel endpoints render platform-specific control UX. Telegram owns command menu publication, inline keyboard rendering, callback token mapping, and message editing. PalCore and Control receive typed interaction results, not Telegram callback payloads.

## MCP

MCP is a first-party detachable plugin backed by a manager sidecar.

The sidecar owns:

- config discovery under `runtime_root/plugins/mcp`
- stdio process/session lifecycle
- initialize and `notifications/initialized`
- tools/prompts pagination
- tool calls and prompt rendering
- discovery snapshots

The Pal plugin owns:

- starting/stopping the sidecar
- refreshing projection
- publishing compiled capabilities
- registering declared MCP prompt skills

Current defaults:

- MCP server request timeout: 300 seconds
- Pal-to-manager IPC timeout: 300 seconds

## Minion

Minion is a detachable first-party builtin plugin. The plugin manifest is provisioned under `runtime_root/plugins/_builtin/minion`; plugin-owned runtime configuration lives under `runtime_root/plugins/minion`.

Minion is a detachable first-party subsystem backed by a manager sidecar and the shared sidecar foundation.

PalCore only sees:

- a minion event source
- minion capabilities
- control action handlers for `minion_approval_decision` and `minion_lesson_decision`

Minion owns tasks, work orders, milestones, checkpoints, ledger, and lesson candidates in its own repository. `op_minion_decision_send` is not exposed to the LLM surface; approval decisions arrive through Control interactions and are routed to the minion module handler.

The manager sidecar pushes events through the `subscribe_events` IPC stream. The Pal-side provider buffers pushed events and calls `core.notify_ready()`. The event source still drains the buffered events through the core loop, but delivery no longer depends on a user turn polling the manager.

Current capability surface:

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

Work order drafts are minion-owned planning artifacts for user brainstorming and module-boundary discussion. They are searchable, but they are not progress truth. The route is draft -> planner review -> user/Pal confirmation -> formal work order. Promotion is explicit through `op_minion_promote_work_order_draft`; `op_minion_spawn` can also accept `draft_id` and let the minion subsystem promote before spawning through the same main entry.

Checkpoint is the milestone cursor fact. Completed checkpoints advance the derived current milestone; partial or blocked checkpoints do not.

Progress and checkpoint events are manager/tasking telemetry. They are written to the minion ledger and checkpoint tables, but they are not direct chat notifications. Pal should answer progress questions by inspecting active runs and work order snapshots. Terminal events are the user-facing completion notification path and also synchronize a recent completion observation into Pal's prompt context.

Minion deliverables are file-first. Coder profiles keep their Git task repo/branch and write reports under `minion_outputs/{work_order_id}`. Planner, reviewer, generic, and other one-shot profiles get a manager-allocated folder workspace under `data/minion/workspaces/{run_id}_{profile}` with `work_order.json`, `metadata.json`, `logs/`, and `deliverables/`. The runner exposes `op_minion_artifact_write`, scoped only to `workspace.artifact_dir`, and terminal/checkpoint payloads carry `artifacts[]` plus `primary_artifact`. Large reports should be read from those files instead of being pushed as chat text.

Terminal event summaries are cleaned before display: `Task Lesson` and `System Lesson` sections are extracted into structured fields and removed from the final completion text. When lessons are present, Pal opens a separate `minion_lesson_approval` interaction with `Accept`, `Reject`, and `Edit` buttons. Accept stores task lessons as tasking continuity, stores system lessons as accepted candidates, and attempts an L3 memory commit when `op_l3_commit_write` is available. Reject discards them. Edit pauses absorption and asks for revised lesson text.

Coder minion task execution is Git-backed by design. A task owns or resolves a task repo, each work order runs on its own branch, and each completed milestone should correspond to a commit on that branch. A completed checkpoint should carry the milestone index, commit SHA, and artifact references. User acceptance/finalization can later squash milestone commits and merge/apply them to the target branch.

Minion profiles are declarative TOML templates plus optional runtime overrides in `runtime_root/plugins/minion/profiles/*.toml`. Builtin profiles use `capability_policy.mode = "inherit_filtered"`: spawn starts from Pal's current capability registry, adds profile defaults/provider hooks, then applies the minion deny policy. `core_minion_read` includes scoped discovery/read/call and read-only `op_l3_recall_query`; `web_research` ensures `op_web_search_query` and `op_web_fetch_read` are available when the slim runner runtime supports them.

Runner capability exposure has a default deny policy. The runner can use task tools, report through events, and request approval, but it does not see `intro_*`, `op_minion_*`, memory-write, behavior/skill mutation, channel/plugin management, or lifecycle attach/detach/rescan style operations. This keeps recursive minion spawning and Pal state mutation out of the runner context.

The internal allowed pool can be broad, but the LLM-facing tool surface stays small. Normal minion runs expose scoped discovery/read/call plus direct work tools when allowed: `op_exec_run`, `op_web_search_query`, `op_web_fetch_read`, and `op_l3_recall_query`. Discovery and read are backed by a scoped runtime view that returns only allowed and non-denied capabilities.

The runner prompt requires `op_l3_recall_query` after a failed tool/capability call when recall is allowed, before retrying, debugging further, or reporting the milestone blocked.

## Capability Boundary

Capability availability is runtime state.

Persisted metadata may explain prior state, but live facts such as attached status, process status, socket status, and capability availability must be inspected live.

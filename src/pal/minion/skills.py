from __future__ import annotations

from pal.skill.contracts import SkillApplicabilitySTAR, SkillDescriptor


PAL_MINION_DEVELOPMENT_SKILL_ID = "pal.minion.development"
PAL_MINION_PROFILE_DEVELOPMENT_SKILL_ID = "pal.minion.profile.development"


PAL_MINION_DEVELOPMENT_MANUAL = """# Pal Minion Development

Use this skill when Pal needs to add, review, repair, or explain the Minion subsystem: workflow dispatch, profiles, gates, scheduler/resource slots, step executor behavior, runner coroutine isolation, workspace environment setup, repair bill replay, sandboxing, or minion-facing internal tools.

## Current Shape

Minion is a detachable first-party subsystem. Keep the roles crisp:

- Pal's main agent owns user conversation, source-scope shaping, workspace fact collection, progress reporting, and user confirmation.
- Task is the durable semantic container. Bind `profile_family` on the task before creating work orders. Work orders are execution attempts under that task; runs are concrete runner executions.
- Before normal delegation, Pal should call `intro_minion_task_search` with the user goal/repo/domain facts. Reuse a matching task_id, or create one with `op_minion_task_create(profile_family=...)`. Then call `op_minion_dispatch_workflow(task_id=...)`.
- `op_minion_dispatch_workflow` is the normal work-order delegation entrypoint. It takes `task_id`, `goal`, optional source acceptance ledger in `requirements_brief`, workspace facts, and optional endpoint. It must not take `profile_name`/`profile_group`; profile family is bound on the task.
- If the user gives numbered requirements, preserve that numbered list in `requirements_brief` as atomic source items. Do not replace it with a prose summary, merge enumerated values, or expand it with Pal/architect-invented features. Dispatch mechanically compiles those items into an immutable source gate contract. Architect coverage evidence must come from plan `gate_check_refs`; downstream coder/reviewer work consumes module and milestone AC from the accepted plan.
- The manager is mechanical orchestration: it creates work orders under tasks, snapshots `profile_family` into work order/workflow metadata, starts the family DAG producer or generic single-node DAG producer, supervises step executor processes, owns resource policy/IPC/notifications/ledger, and persists state through repositories.
- DAG production and DAG consumption are separate. A family-specific producer may use a profile such as `software_engineering.architect`; otherwise the generic producer builds a closed single-node DAG with requirement/context/produce/verify milestones. The executor is resolved from task/family metadata or a family profile fallback, not from dispatch args.
- `dag_advancer` is the only DAG progression mechanism. Keep graph construction, indegree/ready/running/completed/stale transitions, replay merge, invalidation, and dispatch decisions there. Modules, runners, gates, and repair bills report events; they do not advance the DAG directly.
- Manager policy is strategy. Keep policy choices such as auto-advance, concurrency limits, executor resolution, IPC process supervision, and user notification in the manager/control layer.
- Accepted artifacts are DAGs consumed by role profiles. Module/topology metadata such as `executor_profile` is a per-node override for mixed DAGs. The manager reads this metadata mechanically instead of branching on workflow kind.
- Minions do bounded execution. They do not spawn minions. A profile may declare what output unlocks the next workflow step; the manager consumes that declaration.
- Reviewer is special inside the in-process repair loop. Treat `software_engineering.reviewer` as a gate strategy or repair loop participant. Use `software_engineering.review_worker` when a workflow step should produce a standalone review artifact as the final deliverable.
- Domain interactions live with their domain. Minion approval/question/plan interactions belong under `pal.minion`; memory candidate approval belongs under `pal.memory`; `pal.control` transports and renders generic interaction deliveries.

## Main Workflow

For software work, the default flow is:

1. Pal prepares `goal`, optional source acceptance ledger in `requirements_brief`, and `workspace` facts. When the user provides `需求: 1. ... 2. ...`, copy those items as the canonical source ledger. Pal may add constraints, non-goals, acceptance notes, or resolved preferences, but must not weaken or expand the user's numbered source items.
2. Pal searches existing Minion tasks with `intro_minion_task_search`. If no matching long-lived task exists, Pal creates one with `op_minion_task_create`, binding `profile_family` such as `software_engineering`.
3. Pal dispatches a work order with `op_minion_dispatch_workflow(task_id=...)`; dispatch inherits the task family and does not restate profile selectors.
4. The family DAG producer creates the DAG. For software this is normally `software_engineering.architect` using plan builder tools. For families without a producer, the generic producer creates a closed single-node DAG with a few milestones and hands that node to the resolved executor.
5. Architect-generated software plans write the machine plan and a mechanical `plan_review.md` artifact for user review. Generic single-node plans can run directly as parent DAGs.
6. `plan_acceptance` gate reviews software plans. Passing review opens plan acceptance unless `interaction_mode` allows autonomous acceptance.
7. Accepted or mechanically generated plans compile into a module DAG. `dag_advancer` schedules ready nodes under the global LLM-node concurrency limit while the manager grants slots and records ledger state.
8. Each DAG node is consumed by its executor profile. Coder milestones run in isolated contexts and Git worktrees/checkouts. Mutually exclusive phases share one logical slot: architect plus plan review share the plan-production slot, and coder plus gate reviewer share the module slot.
9. Join/final verification publishes back to the project-root work-order branch when the parent DAG completes. Users normally inspect the project root; `.minion` is the internal execution/artifact area.

Non-software profiles usually run as generic single-node DAGs unless their family registers a richer DAG producer. They still use the same pre/in/post shape: prepare environment, execute bounded work, then postprocess/gate/finalize.

## Profile And Workflow Rules

Profiles are extension points, not manager branches.

- Builtin profiles live under `src/pal/minion/profile_templates/`; runtime overrides live under `runtime_root/plugins/minion/profiles/*.toml`.
- Public family selection happens at task creation through `profile_family`. Normal dispatch does not pass `profile_name` or `profile_group`.
- Choose `profile_family` by domain before creating the task: code/repo/review work is `software_engineering`; nutrition, diet, meal planning, training, health check-in, and Nathan coaching tasks are `lifestyle`; use `general` only when no registered domain family fits.
- The task `profile_family` is the default interpretation context for all work orders under that task. Work orders snapshot it into workflow metadata, and bare `workflow_next.profile` names are resolved inside that family before persistence. Keep canonical ids such as `software_engineering.architect` as runtime metadata.
- A family DAG producer decides how to turn source scope into a DAG. It may add boundary contracts, decisions, failure modes, negative cases, and verification criteria that make the source items implementable, but it must not create new functional scope. If user-owned scope is ambiguous, ask a question instead of inventing scope. Use the generic single-node producer when the family has no producer. Do not add dispatch-time profile-selection rules.
- The produced artifact may declare node `executor_profile` values. Use these contracts instead of hard-coding architect -> coder or profile-specific if/else logic.
- A plan module may declare `metadata.executor_profile` or topology `executor_profile` when one node needs a different executor from the DAG default. Prefer a single default executor for ordinary implementation or review DAGs, and prefer group-local profile names unless the node intentionally crosses profile groups.
- Profile `[execution_contract]` declares how a DAG node is materialized for that profile: `module_adapter`, `module_role`, and `artifact_role`. Do not infer coder/reviewer/architect behavior from profile names or prompt text.
- `gate_policy.gates = [...]` declares review requirements by stable gate names.
- Use `gates = ["none"]` for profiles that intentionally complete without review.
- Do not let prompt text be the only contract. If routing, capability exposure, workflow progression, or gate behavior matters, represent it in profile policy, gate definitions, repository metadata, or manager state.

## DAG Advancement And Resource Slots

The manager owns resource policy; `dag_advancer` owns DAG readiness and progression. The global cap is `max_parallel_llm_nodes` (`max_parallel_modules` is a compatibility alias) because LLM provider capacity is the real bottleneck. One logical execution node holds a slot until its mutually exclusive phases finish, so coder and reviewer naturally share the same module slot during repair.

- Build execution order from the accepted plan's module dependencies in `dag_advancer`; do not add separate fork/join flags when the dependency graph already expresses the order.
- Track per-work-order DAG state through repository projections and advancer state: module status, dependency graph, indegree table, ready queue, active children, completed modules, stale/invalidated nodes, and join readiness.
- Resource acquisition should be event-driven. If no slot is available, queue the ready node and wait on slot availability instead of busy-polling.
- Always return slots on normal completion, failure, kill, timeout, and abandoned-run recovery.
- Step execution is per DAG: one step executor child process hosts that DAG's runner coroutines. Each runner coroutine still needs isolated context, transcript, scoped execution runtime, workspace metadata, and Git environment.
- Manager restart must be able to rebuild safe scheduling state from persisted work order metadata, plan DAG projection, active runs, and module statuses.

## Workspace And Environment

Workspace preparation should happen before slow LLM calls whenever possible.

- New projects require a declared `primary_language`; existing repos should be inspected enough to infer the main language and repo path.
- Language-specific setup belongs in workspace environment preparers, not in model troubleshooting. Prefer reusable templates under `plugins/minion/workspace_environment`.
- Coder work is Git-backed and isolated. Each module sees a full checkout/worktree of its baseline under `.minion/worktrees`; do not treat duplicated files across module worktrees as a contract-copy bug.
- Shared contracts must have one canonical producer-owned source path and import/include path. Downstream modules consume that path; they must not create dependency source copies under their own owned areas.
- Keep task repos under runtime data, not `/tmp`, so work survives cleanup and can be inspected after completion. Completed parent DAGs publish the final branch to the project root and keep artifacts under `.minion/artifacts`.

## Workspace Environment Templates

Runtime workspace environment templates belong to Minion. Prefer creating or updating a runtime template under:

```text
<runtime_root>/plugins/minion/workspace_environment/<preparer_id>.toml
```

Do not edit package builtin templates under `src/pal/minion/workspace_environment_templates/` unless the user is intentionally upstreaming a first-party default. Runtime templates override builtin templates with the same `preparer_id`.

The template-backed fallback lets a new language work when metadata advertises a matching `language_ids` entry. Add a workspace environment template when a Minion workspace should prepare runtime env vars, path entries, baseline files, LSP server bindings, or language-specific setup summaries:

```toml
preparer_id = "example-runtime"
kind = "runtime"
language_ids = ["example"]
repo_markers = ["example.toml"]

[env.vars]
EXAMPLE_TEST_MODE = "1"

[[env.path_prepend]]
name = "PYTHONPATH"
path = "src"
if_exists = "src"
```

For baseline repo files:

```toml
preparer_id = "example-lsp"
kind = "lsp"
language_ids = ["example"]
required_lsp_server_ids = ["example_ls"]
server_ids = ["example_ls"]
readonly_skip = "repo already existed; did not create baseline example config"

[[files]]
path = ".example-ls.toml"
skip_if_exists = [".example-ls.toml"]
content = "# generated by Pal\n"
```

Fields:

- `preparer_id`: stable unique id. Runtime templates with the same id override builtin templates.
- `kind`: `runtime` for env vars/path setup, or `lsp`/`workspace` for server summaries and baseline files.
- `language_ids`: canonical ids used by plan metadata and workspace setup.
- `repo_markers`: optional files/directories that must exist before the template applies.
- `required_lsp_server_ids`: LSP templates that must be available before an LSP/workspace template runs.
- `env.vars`: environment variables injected into Minion command execution.
- `env.path_prepend`: per-variable path entries, optionally guarded by `if_exists`, `unless_exists`, `if_language`, or `if_repo_language`.
- `files`: deterministic small baseline files; never overwrite user files, and use `skip_if_exists`.

Add Python code only when one of these is true:

- Module path inference needs new extension aliases in `LANGUAGE_EXTENSIONS`.
- Common user spelling needs a new canonical alias in `LANGUAGE_ALIASES`.
- Review/coder setup needs behavior the declarative workspace environment templates cannot express.

When code is needed, keep generic behavior in `src/pal/minion/workspace_environment.py`, preserve `WorkspaceEnvironmentPreparer` idempotency, and never add language-specific setup back into `git_env.py`.

## Gates

Minion gates are runtime orchestration policy. They decide what must be verified after a milestone result, which reviewer or strategy performs verification, and how failed findings become repair items.

Current builtin gates live in `src/pal/minion/gates.py`:

- `checkpoint_quality`: checkpoint verification for coder-style implementation milestones.
- `plan_acceptance`: architect plan artifact review and optional revision.
- `source_contract`: optional pre-plan source contract compiler for unusually risky or strongly constrained planning tasks.
- `none`: explicit no-gate policy.

Keep these concepts separate:

- Profile TOML chooses gate names through `[gate_policy] gates = [...]`.
- `GateDefinition` owns the gate contract: target kind, gate kind, strategy, reviewer profile, repair/revision bounds, blocking classes, policy flags, and required checklist entry refs.
- `GateChecklistEntry` owns one reusable checklist item. Gate definitions reference entries by stable ids.
- `GateSpec` is the expanded runtime shape derived from profile policy plus gate definitions.
- `GateStrategy` executes the gate. `reviewer` launches a reviewer minion; `none` means no gate.
- Gate results and repair findings are projected into the active todo/repair ledger. Do not make the LLM hand-author that ledger when runtime can derive it.

## Repair Bill Replay

Repair bills are downstream feedback projected back into the plan/module graph.

- Resume and repair are separate paths. Use `op_minion_resume_work_order` when the existing child work order should continue from its next incomplete milestone after provider failure, timeout, interrupted I/O, manager recovery, or stale blocked runner state. Use `op_minion_submit_repair_bill` only for semantic module defects, contract defects, or downstream verification evidence that changes the module obligations.
- A resumed module reuses the same child work order and its milestone cursor. It must not create a replay child such as `_r1`, must not add repair acceptance criteria, and must not invalidate downstream nodes.
- `op_minion_submit_repair_bill` is the public operation for submitting structured replay feedback.
- A repair bill exists because integration, join, or downstream module verification can discover that an earlier module contract, acceptance criterion, or plan boundary was incomplete. It is the reverse-propagation mechanism from downstream evidence back to the affected plan DAG nodes.
- Treat a repair bill as "amended obligations for the existing DAG", not as a new plan. It must preserve the original plan identity, module ids, dependency meaning, and revision history.
- A bill must be module-name indexed and shape-compatible with the existing plan/module schema. Avoid a second truth source.
- Use existing `module_name` values as the primary keys. Do not invent new names, duplicate modules, or describe repairs only in prose.
- Keep patch shape isomorphic to plan shape: module patches attach to modules, acceptance criteria attach to the module or milestone they constrain, evidence attaches to the finding or criterion it proves, and replay scope follows dependency edges.
- Module or contract defects can add acceptance criteria, counterexamples, tests, or repair notes to the affected module and then replay the relevant part of the DAG.
- Integration defects should normally be fixed in the current join/integration context when possible.
- Architecture defects, missing module boundaries, or invalid DAG structure block the parent work order and require user plan/module-boundary review before a replacement DAG epoch. Do not pretend a local repair can fix a bad DAG.
- Replay should merge new obligations into the repository/advancer projection and schedule through the normal DAG, slot, workspace, and gate logic. Do not create a parallel scheduler or repair-only execution path.
- The manager consumes repair bills mechanically. LLMs can produce evidence and proposed patches, but they should not decide hidden replay state outside the structured bill.
- Preserve original plan identity and revision history; replay should merge new obligations rather than mutate old artifacts in place.

## Extension Points

Prefer these extension surfaces before adding manager special cases:

- profiles: `src/pal/minion/profile_templates/` or runtime profile TOML
- workspace environment: `src/pal/minion/workspace_environment.py` and runtime preparer templates
- gates: `GateDefinition`, `GateChecklistEntry`, and `GateStrategy` provider protocols
- plan builder: typed plan operations in `src/pal/minion/plan_builder.py`; domain plugins may register deterministic alias tools with `@plan_builder_alias`/`register_plan_builder_alias` that map domain terminology back to the core plan DAG operations
- scoped tools: `src/pal/minion/scoped_execution.py`
- interactions: `src/pal/minion/interactions.py`
- DAG/resource behavior: `dag_advancer`, manager resource policy, step runner state, and persistent recovery
- sandbox policy: `src/pal/minion/sandbox.py`

Provider protocols for gate extensions:

- `MinionGateChecklistEntryProvider.declared_minion_gate_checklist_entries()`
- `MinionGateDefinitionProvider.declared_minion_gate_definitions()`
- `MinionGateStrategyProvider.declared_minion_gate_strategies()`

Keep provider output deterministic and side-effect free.

## Implementation Workflow

When changing Minion behavior:

1. Identify the boundary first: profile policy, DAG advancement, manager resource/IPC policy, runner execution, gate/review, workspace environment, repository persistence, interaction delivery, or packaging.
2. Read the nearby module README/docs and current tests before editing. Minion has intentionally separated extension points; use them.
3. Keep manager and `dag_advancer` logic mechanical. Do not put LLM reasoning in DAG advancement, slot allocation, recovery, or state transitions.
4. Keep minion contexts isolated. Each logical runner needs its own prompt state, scoped runtime, workspace, artifact directory, and Git environment.
5. Keep user-facing interactions domain-owned and delivered through control. Do not put Minion-specific renderers in `pal.control` or channel endpoints.
6. If failed gate findings or repair bills should drive repairs, update the repair/todo ledger projection code instead of relying on reviewer prompt wording alone.
7. Update `docs/pal_minion_v1.md` and `docs/current_implementation_notes.md` when product behavior changes.
8. Update `scripts/build_package.sh` when a new builtin profile, gate, scheduler contract, internal skill route, workspace template, or package file must be present in release wheels.

Prefer small typed runtime changes over prompt-only behavior. The LLM should operate inside contracts; it should not be asked to infer hidden scheduler, workspace, or gate semantics.

## Test Targets

Add focused tests near the changed layer. Do not run the full suite in one shot unless explicitly requested.

- Profile tests: builtin/runtime profiles list, read, expand capability groups, gate policy, and `workflow_next` correctly.
- Task/dispatch tests: `op_minion_task_create` binds `profile_family`, task search can find durable tasks, `op_minion_dispatch_workflow(task_id=...)` inherits task family, conflicting family args are rejected, workspace facts are validated, removed `requirements_review` is rejected, and removed spawn/submit/accept/revise capabilities remain absent.
- DAG/resource tests: indegree rebuild, ready queue scheduling, stale/downstream invalidation, global LLM-node slot acquisition/release, kill/failure recovery, and module completion events.
- Plan builder tests: alias tools map to core DAG operations, submitted plans write `plan_review.md`, and completion writes a work-order `completion_report.md`.
- Runner/coroutine tests: independent context per logical coder, isolated scoped tools, isolated Git/workspace metadata, and clean LLM request hooks.
- Workspace tests: language environment preparation happens before runner LLM calls and does not overwrite existing repo files unexpectedly.
- Gate tests: `normalize_gate_policy(...)`, checklist refs, reviewer submission validation, pass/fail/repair max attempts, and active todo/repair ledger projection.
- Repair bill tests: module-key merge, replay selection, architecture-defect block, active child invalidation, and plan revision history preservation.
- Packaging tests: `scripts/build_package.sh` includes builtin profiles, workspace templates, gates, scheduler, sandbox, and this internal skill.

## Verification Checklist

Before calling a Minion subsystem change done:

1. Public entrypoints stay task-first: search/read task, create task when needed, then dispatch work orders with `op_minion_dispatch_workflow(task_id=...)`; removed lower-level public operations remain absent.
2. Pal/main-agent source-scope shaping and manager mechanical orchestration remain separate.
3. Profile policy or gate definitions express behavior instead of manager hard-coding whenever possible.
4. Module/coder contexts, worktrees, artifacts, and scoped runtimes remain isolated.
5. Resource slots are returned on every terminal path and can be reconstructed after manager restart.
6. Gate failures and repair bills project into structured repository state rather than prose-only prompts.
7. Domain interactions stay under `pal.minion` or the owning domain, not `pal.control`.
8. Focused tests and `scripts/build_package.sh` cover the changed contract.
"""


PAL_MINION_PROFILE_DEVELOPMENT_MANUAL = """# Pal Minion Profile Development

Use this skill when Pal needs to create, review, repair, or explain a Minion profile TOML file, profile capability policy, workspace policy, gate policy, or artifact/profile workflow transition.

## Boundary

A Minion profile describes one bounded executor role. It should not be a hidden manager branch.

- The manager starts profiles and consumes profile policy. It owns resource policy, gates, IPC supervision, and workflow continuation; `dag_advancer` owns DAG progression.
- The profile owns identity, behavior prompt, capability exposure, workspace expectations, output contract, and declared next workflow step.
- A minion running a profile does not spawn other minions. Use artifact `workflow_next` plus profile `output_policy.workflow_next` policy so the manager can mechanically dispatch the next step.
- Pal's main agent owns source-scope shaping. Do not recreate the removed requirements-review profile.
- Prefer runtime profile files under `runtime_root/plugins/minion/profiles/<group>/<profile>.toml` for local experiments; edit `src/pal/minion/profile_templates/` only for builtin product profiles.

## Profile TOML Shape

A profile should declare:

```toml
profile_id = "nutritionist"
profile_group = "lifestyle"
display_name = "Nutritionist Minion"
preferred_endpoint_id = ""
skill_refs = []
capability_groups = ["core_minion_read", "minion_artifacts"]

[workspace_policy]
mode = "folder"

[workspace_environment]
runtime = false
lsp = false
write_baseline_config = false

[capability_policy]
mode = "profile_only"

[execution_contract]
module_adapter = "prompt_view"
module_role = "nutritionist"
artifact_role = "nutrition_plan"

[gate_policy]
gates = ["none"]

[output_policy]
primary_artifact = "nutrition_plan.md"
allowed_output_types = ["MarkdownReport"]
workflow_next = "none"
```

Use triple-quoted `identity_fragment`, `behavior_fragment`, and `output_contract_fragment` for profile instructions. Keep them role-specific and short enough that the runner can still carry task context.

## Policy Choices

Choose policies deliberately:

- `workspace_policy.mode = "folder"` for one-shot report/research/professional profiles.
- `workspace_policy.mode = "read_only_repo"` for architect/reviewer-style inspection.
- Git-backed code work belongs to coder-style profiles and manager-prepared module worktrees; do not make arbitrary profiles mutate source repos.
- `capability_policy.mode = "profile_only"` means the profile TOML is the upper bound.
- `capability_policy.mode = "inherit_filtered"` starts from manager-visible capabilities, adds profile defaults/provider hooks, then applies the minion deny policy. Use it only when the profile genuinely needs Pal's current tool surface.
- `execution_contract.module_adapter` is normally `prompt_view` for artifact/report/review roles and `coder_work_order` for implementation profiles that consume plan modules through the coder repair loop.
- `execution_contract.module_role` and `artifact_role` are runtime contracts. Runner/repository/gate code should read them instead of checking whether a profile name contains `coder`, `reviewer`, or `architect`.
- `gate_policy.gates = ["none"]` is correct for bounded one-shot profiles that finish with an artifact and no reviewer loop.
- Software architecture profiles use `gates = ["plan_acceptance"]`; implementation profiles use checkpoint/module-quality gates. Any gate that launches a reviewer must explicitly name its reviewer profile in policy or gate definition; there is no implicit software reviewer fallback.
- Artifact `workflow_next = "none"` or `output_policy.workflow_next = "none"` ends the workflow. Use a profile id only when the artifact output is meant to mechanically unlock a next step.

## Capability Groups

Prefer existing capability groups over ad hoc tool lists:

- `core_minion_read`: scoped discovery/read/call and read-only memory recall.
- `workspace_read`: read-only workspace inspection tools.
- `workspace_write`: source/workspace mutation tools; use sparingly and only for implementation profiles.
- `minion_artifacts`: artifact write/edit tools under `workspace.artifact_dir`.
- `minion_plan_builder`: architect-only plan builder tools.
- `minion_review_gate`: reviewer gate submission tools.
- `web_research`: web search/read when web research is part of the role and budget/approval policy allows it.

Do not expose `op_minion_*`, memory-write, behavior/skill mutation, channel/plugin management, lifecycle attach/detach/rescan, or recursive delegation tools to runner profiles unless a typed runtime policy intentionally allows it.

## Prompt Contract

Profile prompt fragments should say what the role does and what it must produce, not how manager scheduling works.

- `identity_fragment`: role and authority boundary.
- `behavior_fragment`: execution style, evidence expectations, question policy, and scope limits.
- `output_contract_fragment`: required artifact, final message shape, and any machine-readable fields.
- Avoid asking the profile to request plan approval, spawn coders, mutate Pal config, write memory directly, or infer hidden workflow state.
- For non-code professional profiles, require file-first deliverables with `artifact_write` and a short final summary pointing to the artifact.
- For software profiles, preserve the architect/coder/reviewer split. Architect plans; coder implements assigned modules; reviewer submits gate verdicts. Standalone review workflows should use `review_worker`, not the gate-only `reviewer`.

## Workflow Next

`workflow_next` is the artifact/profile-composition hook.

- Use `none` for terminal artifacts or terminal profiles.
- Use a canonical profile id, such as `software_engineering.coder`, only when the output intentionally crosses profile groups or needs to be unambiguous outside the current family. A bare profile name such as `coder` is resolved against the workflow `profile_family` and then persisted as a canonical id.
- Do not encode continuation rules only in prose. If the manager must act on the result, the policy must be in TOML/output metadata and backed by tests.
- Reviewer repair loops should remain gate strategy behavior, not profile-to-profile workflow chains.

## Runtime Profile Workflow

For local or user-specific profiles:

1. Create `runtime_root/plugins/minion/profiles/<group>/<profile>.toml`.
2. Pick a stable `profile_group` and `profile_id`; avoid colliding with builtin profiles unless intentionally overriding.
3. Start with `profile_only`, `folder`, `execution_contract.module_adapter = "prompt_view"`, `gates = ["none"]`, and profile `workflow_next = "none"` unless the profile proves it needs more.
4. Add only the minimum capability groups needed for the role.
5. Use `intro_minion_profile_list` and `intro_minion_profile_read` to confirm discovery and rendered policy.
6. Dogfood through the task-first path: `intro_minion_task_search`, then `op_minion_task_create(profile_family=<group>)` when no matching task exists, then `op_minion_dispatch_workflow(task_id=...)` without profile selectors.

For builtin profiles:

1. Add the TOML under `src/pal/minion/profile_templates/<group>/<profile>.toml`.
2. Update package data/build checks when the profile must ship in release wheels.
3. Add focused tests for profile listing, reading, capability policy, workspace policy, gate policy, and output policy.
4. Update docs only when the profile becomes supported product behavior.

## Verification Checklist

Before calling a new profile done:

1. `intro_minion_profile_list` shows only `profile_group`, `profile_name`, and `description_summary`.
2. `intro_minion_profile_read` shows expected fragments, workspace policy, capability groups, execution contract, gate policy, and output policy.
3. The profile does not expose recursive minion dispatch or Pal mutation capabilities.
4. Workspace mode matches the role and does not require source mutation unless the role is explicitly an implementation profile.
5. Gate policy is explicit, including `["none"]` for no-gate profiles.
6. Artifact/profile `workflow_next` behavior is explicit, family-scoped bare names are intentional, and non-terminal routing is covered by a focused test.
7. The profile can be dispatched through a task-bound `op_minion_dispatch_workflow(task_id=...)` without lower-level spawn/submit/accept capabilities.
8. Package checks include the profile when it is builtin.
"""


def minion_declared_skills(*, module_id: str = "minion") -> tuple[SkillDescriptor, ...]:
    return (
        SkillDescriptor(
            skill_id=PAL_MINION_DEVELOPMENT_SKILL_ID,
            module_id=module_id,
            title="Pal Minion Development",
            summary=(
                "Develop and validate Minion workflow dispatch, profiles, gates, DAG advancement/resource slots, "
                "workspace setup, runner isolation, and repair bill replay."
            ),
            manual_text=PAL_MINION_DEVELOPMENT_MANUAL,
            activation_terms=(
                "minion development",
                "minion workflow",
                "dispatch_workflow",
                "workflow_next",
                "minion profile",
                "minion scheduler",
                "dag advancer",
                "dag advancement",
                "resource slot",
                "step executor",
                "runner coroutine",
                "repair bill",
                "repair replay",
                "workspace environment",
                "minion gate",
                "gate policy",
                "gate definition",
                "checkpoint_quality",
                "plan_acceptance",
                "reviewer gate",
                "repair loop",
                "acceptance checklist",
                "GateDefinition",
                "GateChecklistEntry",
                "GateSpec",
                "active_gate_todo",
                "gate ledger",
            ),
            capability_refs=(
                "intro_minion_task_search",
                "intro_minion_task_read",
                "op_minion_task_create",
                "op_minion_dispatch_workflow",
                "op_minion_configure",
                "op_minion_resume_work_order",
                "op_minion_submit_repair_bill",
                "op_minion_review_gate_submit",
                "intro_minion_profile_list",
                "intro_minion_work_order_read",
            ),
            applicability_star=SkillApplicabilitySTAR(
                situation="Pal needs to add or repair Minion subsystem behavior or make a profile/gate/scheduler extension durable.",
                task="Find the right extension boundary, update typed runtime policy, wire profiles/gates/workflow, and verify isolation and replay behavior.",
                action="Use the Minion boundary map, workflow rules, scheduler slot rules, gate rules, repair bill rules, and focused test checklist.",
                result="The change is profile-driven where possible, mechanically scheduled, runner-isolated, package-checked, and documented.",
            ),
            use_when=(
                "Use when the user asks Pal to change Minion workflow, profiles, gates, scheduler/concurrency, step executor behavior, runner coroutine isolation, "
                "workspace environment setup, repair bill replay, sandbox policy, or internal Minion developer guidance."
            ),
            avoid_when=(
                "Avoid for ordinary delegated Minion task execution, one-off review reports, LSP template-only work, channel providers, "
                "or plugin work that does not change Minion subsystem semantics."
            ),
            source_format="internal_skill",
            source_refs=(
                "pal.minion.skills",
                "src/pal/minion/capabilities.py",
                "src/pal/minion/manager.py",
                "src/pal/minion/step_runner.py",
                "src/pal/minion/gates.py",
                "src/pal/minion/review_orchestrator.py",
                "src/pal/minion/profile_templates/",
                "docs/pal_minion_v1.md",
            ),
            metadata={"internal": True, "may_require_code_changes": True, "extension_boundary": "minion"},
        ),
        SkillDescriptor(
            skill_id=PAL_MINION_PROFILE_DEVELOPMENT_SKILL_ID,
            module_id=module_id,
            title="Pal Minion Profile Development",
            summary=(
                "Create and validate Minion profile TOML files, capability policy, workspace policy, gate policy, "
                "output contracts, and workflow_next behavior."
            ),
            manual_text=PAL_MINION_PROFILE_DEVELOPMENT_MANUAL,
            activation_terms=(
                "minion profile development",
                "new minion profile",
                "create minion profile",
                "profile toml",
                "profile templates",
                "profile_group",
                "profile_id",
                "capability_groups",
                "capability_policy",
                "workspace_policy",
                "workspace_environment",
                "output_policy",
                "workflow_next",
                "gates none",
                "runtime_root/plugins/minion/profiles",
                "profile_only",
                "inherit_filtered",
                "minion_artifacts",
            ),
            capability_refs=(
                "intro_minion_task_search",
                "op_minion_task_create",
                "intro_minion_profile_list",
                "intro_minion_profile_read",
                "op_minion_dispatch_workflow",
            ),
            applicability_star=SkillApplicabilitySTAR(
                situation="Pal needs to create, repair, or review a Minion profile definition.",
                task="Write or update profile TOML with the right fragments, capability groups, workspace policy, gate policy, and workflow_next.",
                action="Use the profile TOML shape, policy choices, prompt contract rules, runtime/builtin workflow, and verification checklist.",
                result="The profile is discoverable, minimally scoped, dispatchable through a task-bound op_minion_dispatch_workflow, and package-checked when builtin.",
            ),
            use_when=(
                "Use when the user asks Pal to create a new Minion profile, add a runtime profile TOML, promote a profile to builtin, "
                "repair profile policy, or explain how Minion profile composition should work."
            ),
            avoid_when=(
                "Avoid for changing manager scheduling, runner internals, gate strategy code, or workspace language setup itself; "
                "use the broader Minion or LSP development skills for those."
            ),
            source_format="internal_skill",
            source_refs=(
                "pal.minion.skills",
                "src/pal/minion/profiles.py",
                "src/pal/minion/profile_templates/",
                "src/pal/minion/capabilities.py",
                "docs/pal_minion_v1.md",
            ),
            metadata={
                "internal": True,
                "may_require_code_changes": True,
                "extension_boundary": "minion.profiles",
                "runtime_root_layout": "plugins/minion/profiles/<group>/<profile>.toml",
            },
        ),
    )

from __future__ import annotations

from pal.skill.contracts import SkillApplicabilitySTAR, SkillDescriptor


PAL_MINION_DEVELOPMENT_SKILL_ID = "pal.minion.development"
PAL_MINION_PROFILE_DEVELOPMENT_SKILL_ID = "pal.minion.profile.development"


PAL_MINION_DEVELOPMENT_MANUAL = """# Pal Minion Development

Use this skill when Pal needs to add, review, repair, or explain the Minion subsystem: workflow dispatch, profiles, gates, scheduler/resource slots, coroutine runner behavior, workspace environment setup, repair bill replay, sandboxing, or minion-facing internal tools.

## Current Shape

Minion is a detachable first-party subsystem. Keep the roles crisp:

- Pal's main agent owns user conversation, requirements shaping, workspace fact collection, progress reporting, and user confirmation.
- `op_minion_dispatch_workflow` is the normal public delegation entrypoint. Do not reintroduce public `op_minion_spawn`, `op_minion_submit_plan`, `op_minion_accept_plan`, or `op_minion_revise_plan`.
- The manager is mechanical orchestration: it creates work orders, starts the initial profile step, follows profile-declared `workflow_next`, applies gates, updates DAG state, and manages resource slots.
- Minions do bounded execution. They do not spawn minions. A profile may declare what output unlocks the next workflow step; the manager consumes that declaration.
- Reviewer is special inside the in-process repair loop. Treat reviewer work as a gate strategy or repair loop participant, not as an independent user-facing workflow step unless a profile explicitly produces a standalone review artifact.
- Domain interactions live with their domain. Minion approval/question/plan interactions belong under `pal.minion`; memory candidate approval belongs under `pal.memory`; `pal.control` transports and renders generic interaction deliveries.

## Main Workflow

For software work, the default flow is:

1. Pal prepares `goal`, optional `requirements_brief`, and `workspace` facts.
2. Manager dispatches the initial profile, normally `software_engineering.architect`.
3. Architect uses plan builder tools and submits a canonical plan artifact. Pal's main agent does not hand-write plan JSON.
4. `plan_acceptance` gate reviews the plan. Passing review opens plan acceptance unless `interaction_mode` allows autonomous acceptance.
5. Accepted plans compile into a module DAG. The manager schedules ready modules under the global concurrency limit.
6. Coder milestones run in isolated contexts and Git worktrees/checkouts. Reviewer gates run as module-local repair loops and share the module slot.
7. Join/final verification is the only final product worktree that users normally need to inspect.

Non-software profiles can be one-step workflows by declaring `workflow_next = "none"` or equivalent output policy. They still use the same pre/in/post shape: prepare environment, execute bounded work, then postprocess/gate/finalize.

## Profile And Workflow Rules

Profiles are extension points, not manager branches.

- Builtin profiles live under `src/pal/minion/profile_templates/`; runtime overrides live under `runtime_root/plugins/minion/profiles/*.toml`.
- Public profile selection is `profile_group` plus `profile_name`. Keep canonical ids such as `software_engineering.architect` as runtime metadata.
- `output_policy.workflow_next` declares the next stage. Use it instead of hard-coding architect -> coder or profile-specific if/else logic.
- `gate_policy.gates = [...]` declares review requirements by stable gate names.
- Use `gates = ["none"]` for profiles that intentionally complete without review.
- Do not let prompt text be the only contract. If routing, capability exposure, workflow progression, or gate behavior matters, represent it in profile policy, gate definitions, repository metadata, or manager state.

## Scheduler And Resource Slots

The manager owns concurrency. A module occupies one global slot from module start until the module is finished, so coder and reviewer naturally share the same slot during repair.

- Build execution order from the accepted plan's module dependencies; do not add separate fork/join flags when the dependency graph already expresses the order.
- Track per-work-order DAG state: module status, dependency graph, indegree table, ready queue, active children, completed modules, and join readiness.
- Resource acquisition should be event-driven. If no slot is available, queue the ready module instead of busy-polling.
- Always return slots on normal completion, failure, kill, timeout, and abandoned-run recovery.
- Coroutine runner mode should preserve per-coder isolation: each logical runner needs its own context, transcript, scoped execution runtime, workspace metadata, and Git environment.
- Manager restart must be able to rebuild safe scheduling state from persisted work order metadata, plan DAG projection, active runs, and module statuses.

## Workspace And Environment

Workspace preparation should happen before slow LLM calls whenever possible.

- New projects require a declared `primary_language`; existing repos should be inspected enough to infer the main language and repo path.
- Language-specific setup belongs in workspace environment preparers, not in model troubleshooting. Prefer reusable templates under `plugins/minion/workspace_environment`.
- Coder work is Git-backed and isolated. Each module sees a full checkout/worktree of its baseline; do not treat duplicated files across module worktrees as a contract-copy bug.
- Shared contracts must have one canonical producer-owned source path and import/include path. Downstream modules consume that path; they must not create dependency source copies under their own owned areas.
- Keep task repos under runtime data, not `/tmp`, so work survives cleanup and can be inspected after completion.

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

- `op_minion_submit_repair_bill` is the public operation for submitting structured replay feedback.
- A repair bill exists because integration, join, or downstream module verification can discover that an earlier module contract, acceptance criterion, or plan boundary was incomplete. It is the reverse-propagation mechanism from downstream evidence back to the affected plan DAG nodes.
- Treat a repair bill as "amended obligations for the existing DAG", not as a new plan. It must preserve the original plan identity, module ids, dependency meaning, and revision history.
- A bill must be module-key indexed and shape-compatible with the existing plan/module schema. Avoid a second truth source.
- Use existing `module_id`/`module_key` values as the primary keys. Do not invent new names, duplicate modules, or describe repairs only in prose.
- Keep patch shape isomorphic to plan shape: module patches attach to modules, acceptance criteria attach to the module or milestone they constrain, evidence attaches to the finding or criterion it proves, and replay scope follows dependency edges.
- Module or contract defects can add acceptance criteria, counterexamples, tests, or repair notes to the affected module and then replay the relevant part of the DAG.
- Integration defects should normally be fixed in the current join/integration context when possible.
- Architecture defects, missing module boundaries, or invalid DAG structure should pause for architecture revision instead of pretending a local repair can fix the plan.
- Replay should merge new obligations into the manager/repository projection and schedule through the normal DAG, slot, workspace, and gate logic. Do not create a parallel scheduler or repair-only execution path.
- The manager consumes repair bills mechanically. LLMs can produce evidence and proposed patches, but they should not decide hidden replay state outside the structured bill.
- Preserve original plan identity and revision history; replay should merge new obligations rather than mutate old artifacts in place.

## Extension Points

Prefer these extension surfaces before adding manager special cases:

- profiles: `src/pal/minion/profile_templates/` or runtime profile TOML
- workspace environment: `src/pal/minion/workspace_environment.py` and runtime preparer templates
- gates: `GateDefinition`, `GateChecklistEntry`, and `GateStrategy` provider protocols
- plan builder: typed plan operations in `src/pal/minion/plan_builder.py`
- scoped tools: `src/pal/minion/scoped_execution.py`
- interactions: `src/pal/minion/interactions.py`
- scheduler/resource behavior: manager plus scheduler/step runner state, with persistent recovery
- sandbox policy: `src/pal/minion/sandbox.py`

Provider protocols for gate extensions:

- `MinionGateChecklistEntryProvider.declared_minion_gate_checklist_entries()`
- `MinionGateDefinitionProvider.declared_minion_gate_definitions()`
- `MinionGateStrategyProvider.declared_minion_gate_strategies()`

Keep provider output deterministic and side-effect free.

## Implementation Workflow

When changing Minion behavior:

1. Identify the boundary first: profile policy, manager scheduling, runner execution, gate/review, workspace environment, repository persistence, interaction delivery, or packaging.
2. Read the nearby module README/docs and current tests before editing. Minion has intentionally separated extension points; use them.
3. Keep manager logic mechanical. Do not put LLM reasoning in scheduler, slot allocation, recovery, or DAG state transitions.
4. Keep minion contexts isolated. Each logical runner needs its own prompt state, scoped runtime, workspace, artifact directory, and Git environment.
5. Keep user-facing interactions domain-owned and delivered through control. Do not put Minion-specific renderers in `pal.control` or channel endpoints.
6. If failed gate findings or repair bills should drive repairs, update the repair/todo ledger projection code instead of relying on reviewer prompt wording alone.
7. Update `docs/pal_minion_v1.md` and `docs/current_implementation_notes.md` when product behavior changes.
8. Update `scripts/build_package.sh` when a new builtin profile, gate, scheduler contract, internal skill route, workspace template, or package file must be present in release wheels.

Prefer small typed runtime changes over prompt-only behavior. The LLM should operate inside contracts; it should not be asked to infer hidden scheduler, workspace, or gate semantics.

## Test Targets

Add focused tests near the changed layer. Do not run the full suite in one shot unless explicitly requested.

- Profile tests: builtin/runtime profiles list, read, expand capability groups, gate policy, and `workflow_next` correctly.
- Dispatch tests: `op_minion_dispatch_workflow` validates workspace facts, rejects removed `requirements_review`, and does not expose removed spawn/submit/accept/revise capabilities.
- Scheduler tests: indegree rebuild, ready queue scheduling, global slot acquisition/release, kill/failure recovery, and module completion events.
- Runner/coroutine tests: independent context per logical coder, isolated scoped tools, isolated Git/workspace metadata, and clean LLM request hooks.
- Workspace tests: language environment preparation happens before runner LLM calls and does not overwrite existing repo files unexpectedly.
- Gate tests: `normalize_gate_policy(...)`, checklist refs, reviewer submission validation, pass/fail/repair max attempts, and active todo/repair ledger projection.
- Repair bill tests: module-key merge, replay selection, architecture-defect pause, and plan revision history preservation.
- Packaging tests: `scripts/build_package.sh` includes builtin profiles, workspace templates, gates, scheduler, sandbox, and this internal skill.

## Verification Checklist

Before calling a Minion subsystem change done:

1. Public entrypoints still center on `op_minion_dispatch_workflow`; removed lower-level public operations remain absent.
2. Pal/main-agent requirements shaping and manager mechanical orchestration remain separate.
3. Profile policy or gate definitions express behavior instead of manager hard-coding whenever possible.
4. Module/coder contexts, worktrees, artifacts, and scoped runtimes remain isolated.
5. Resource slots are returned on every terminal path and can be reconstructed after manager restart.
6. Gate failures and repair bills project into structured repository state rather than prose-only prompts.
7. Domain interactions stay under `pal.minion` or the owning domain, not `pal.control`.
8. Focused tests and `scripts/build_package.sh` cover the changed contract.
"""


PAL_MINION_PROFILE_DEVELOPMENT_MANUAL = """# Pal Minion Profile Development

Use this skill when Pal needs to create, review, repair, or explain a Minion profile TOML file, profile capability policy, workspace policy, gate policy, or profile-declared workflow transition.

## Boundary

A Minion profile describes one bounded executor role. It should not be a hidden manager branch.

- The manager starts profiles and consumes profile policy. It owns scheduling, gates, resource slots, and workflow continuation.
- The profile owns identity, behavior prompt, capability exposure, workspace expectations, output contract, and declared next workflow step.
- A minion running a profile does not spawn other minions. Use `output_policy.workflow_next` so the manager can mechanically dispatch the next step.
- Pal's main agent owns requirements shaping. Do not recreate the removed planner/requirements-review profile.
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
- `gate_policy.gates = ["none"]` is correct for bounded one-shot profiles that finish with an artifact and no reviewer loop.
- Software architecture profiles use `gates = ["plan_acceptance"]`; coder profiles use checkpoint/module-quality gates.
- `output_policy.workflow_next = "none"` ends the workflow. Use `software_engineering.coder` or another canonical profile id only when the profile output is meant to mechanically unlock a next step.

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
- For software profiles, preserve the architect/coder/reviewer split. Architect plans; coder implements assigned modules; reviewer submits gate verdicts.

## Workflow Next

`workflow_next` is the profile-composition hook.

- Use `none` for terminal profiles.
- Use a canonical profile id, such as `software_engineering.coder`, only when the output has a typed artifact/gate that the manager can consume.
- Do not encode continuation rules only in prose. If the manager must act on the result, the policy must be in TOML/output metadata and backed by tests.
- Reviewer repair loops should remain gate strategy behavior, not profile-to-profile workflow chains.

## Runtime Profile Workflow

For local or user-specific profiles:

1. Create `runtime_root/plugins/minion/profiles/<group>/<profile>.toml`.
2. Pick a stable `profile_group` and `profile_id`; avoid colliding with builtin profiles unless intentionally overriding.
3. Start with `profile_only`, `folder`, `gates = ["none"]`, and `workflow_next = "none"` unless the profile proves it needs more.
4. Add only the minimum capability groups needed for the role.
5. Use `intro_minion_profile_list` and `intro_minion_profile_read` to confirm discovery and rendered policy.
6. Dogfood with `op_minion_dispatch_workflow` using `profile_group` and `profile_name`.

For builtin profiles:

1. Add the TOML under `src/pal/minion/profile_templates/<group>/<profile>.toml`.
2. Update package data/build checks when the profile must ship in release wheels.
3. Add focused tests for profile listing, reading, capability policy, workspace policy, gate policy, and output policy.
4. Update docs only when the profile becomes supported product behavior.

## Verification Checklist

Before calling a new profile done:

1. `intro_minion_profile_list` shows only `profile_group`, `profile_name`, and `description_summary`.
2. `intro_minion_profile_read` shows expected fragments, workspace policy, capability groups, gate policy, and output policy.
3. The profile does not expose recursive minion dispatch or Pal mutation capabilities.
4. Workspace mode matches the role and does not require source mutation unless the role is explicitly an implementation profile.
5. Gate policy is explicit, including `["none"]` for no-gate profiles.
6. `workflow_next` is explicit and covered by a focused test when it is not `none`.
7. The profile can be dispatched through `op_minion_dispatch_workflow` without lower-level spawn/submit/accept capabilities.
8. Package checks include the profile when it is builtin.
"""


def minion_declared_skills(*, module_id: str = "minion") -> tuple[SkillDescriptor, ...]:
    return (
        SkillDescriptor(
            skill_id=PAL_MINION_DEVELOPMENT_SKILL_ID,
            module_id=module_id,
            title="Pal Minion Development",
            summary=(
                "Develop and validate Minion workflow dispatch, profiles, gates, scheduler/resource slots, "
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
                "resource slot",
                "coroutine runner",
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
                "op_minion_dispatch_workflow",
                "op_minion_configure",
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
                "Use when the user asks Pal to change Minion workflow, profiles, gates, scheduler/concurrency, coroutine runner, "
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
                "intro_minion_profile_list",
                "intro_minion_profile_read",
                "op_minion_dispatch_workflow",
            ),
            applicability_star=SkillApplicabilitySTAR(
                situation="Pal needs to create, repair, or review a Minion profile definition.",
                task="Write or update profile TOML with the right fragments, capability groups, workspace policy, gate policy, and workflow_next.",
                action="Use the profile TOML shape, policy choices, prompt contract rules, runtime/builtin workflow, and verification checklist.",
                result="The profile is discoverable, minimally scoped, dispatchable through op_minion_dispatch_workflow, and package-checked when builtin.",
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

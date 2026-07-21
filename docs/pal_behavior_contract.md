# Pal Behavior Contract

> V1 contract for affordance + skill routing.

`behavior` is Pal's scenario-to-action routing subsystem. It answers:

- what should Pal remember it can do in this situation?
- which skill manual may help?
- which capability refs are relevant?
- which memory queries may be useful later?

It does not execute capabilities, does not trigger memory recall, does not own skill lifecycle, and does not own approval. Actual side effects still go through `Execution`.

## Concept Boundaries

### Capability

`Capability` is runtime truth.

It describes an executable or observable capability that exists in the current runtime and can be invoked through `Execution`.

Capability answers:

- what exists?
- how is it invoked?
- where is the runtime handler?

### Skill

`Skill` is a manual or playbook owned by the `skill` subsystem.

It tells Pal how to complete a class of behavior safely, but it is not executable by itself.

Skill answers:

- what steps should Pal follow?
- which capabilities are usually involved?
- where should Pal stop and ask the user?

### Affordance

`Affordance` is a behavior routing hint.

It tells Pal when a capability or skill should come to mind.

Affordance answers:

- when this scenario appears, what should Pal consider?
- which capability refs or skill refs are relevant?
- how confident is this route?

### Memory Case

`Memory case` is task experience.

It records concrete previous situations and outcomes. It must not be mixed into affordance search. A memory case may help solve a task, while an affordance helps Pal remember that a behavior path exists.

## Subsystem Ownership

`pal.behavior` owns:

- affordance descriptors
- behavior advice retrieval and ranking
- resident affordance prompt hints

`pal.skill` owns:

- skill descriptors
- skill assimilation and sanitization
- skill storage and versioning
- skill manual injection

`PalCore` owns:

- module composition
- prompt assembly
- tool surface exposure
- orchestration between behavior, execution, memory, channel, and control

`Execution` owns:

- capability registry
- capability dispatch
- real tool invocation
- approval enforcement at the execution/capability layer

## Descriptor Model

### `AffordanceDescriptor`

Core fields:

- `affordance_id`
- `module_id`
- `title`
- `scenario_text`
- `activation_terms`
- `prompt_hint`
- `visibility_mode`
- `activation_kind`
- `activation_mode`
- `source_kind`
- `capability_refs`
- `skill_refs`
- `memory_query_hints`
- `priority`
- `activation_threshold`
- `enabled`

`visibility_mode`:

- `resident`: may enter short resident prompt hints.
- `discoverable`: only found through `behavior_advise`.

`activation_kind`:

- `deliberative`: model-facing behavioral advice.
- `reactive`: runtime-event route for future direct event handling.

`activation_mode`:

- `suggest`: return as a candidate route.
- `automatic`: future automatic route hint.
- `require_approval`: the route may involve approval-gated capabilities.

`activation_mode=require_approval` does not create a separate approval path. Approval is still enforced only by capability execution policy.

`source_kind`:

- `declared`: plugin/provider/module declares the default route.
- `instructed`: user explicitly teaches Pal a route.
- `learned`: Pal derives a candidate route from repeated success.

### `SkillDescriptor`

`SkillDescriptor` is defined by `pal.skill`.

Behavior only stores and returns `skill_refs`. It may use active skill metadata for routing, but it must not own skill content or lifecycle.

## Source And Lifecycle Rules

### Declared

Declared affordances and skills are tied to module lifecycle.

When a module or plugin publishes capabilities:

- explicit `@affordance(...)` declarations are registered.
- explicit `@skill(...)` declarations are registered.
- capability descriptors are also auto-indexed as declared affordances.

When a module or plugin is detached:

- declared affordances are removed from search.
- declared skills are removed from injection.

### Instructed And Learned

Instructed and learned affordances are persistent behavior state.

They do not disappear when a plugin is detached. If their referenced capabilities become unavailable, advice may still return them, but the candidate availability must be `partial` or `unavailable`.

## Availability Rules

For `capability_refs`:

- all refs resolve in the execution registry: `available`
- some refs resolve: `partial`
- no refs resolve: `unavailable`

Unavailable refs do not block advice. They must be reported explicitly so Pal can decide whether to install, rescan, avoid, or ask the user.

## Confidence Rules

V1 confidence uses deterministic ranking:

- lexical match
- source prior
- priority

Source prior:

- `instructed`: strongest
- `declared`: medium/high
- `learned`: weakest

Candidates below `activation_threshold` are not returned.

Learned affordances must use weak wording even when the lexical score is high. They may say "consider" or "maybe", but must not imply "must", "always", or strong obligation.

## Public Tools

### `behavior_advise`

Async-first behavior consultation.

Input:

- `scenario`
- `intent`
- `turn_kind`
- `constraints`
- `already_considered`
- `top_k`

Output:

- route candidates
- confidence
- availability
- source kind
- capability refs
- skill refs
- memory query hints

Rules:

- does not execute capabilities
- does not inject skills
- does not trigger memory recall
- does not suggest recursively calling `behavior_advise`
- sync invoke returns structured failure: `async_required`
- internal semantic router failure falls back to deterministic ranking

### `skill_inject`

Skill manual injection.

Input:

- `skill_id`

Output:

- full `manual_text`
- skill metadata
- capability refs

Rules:

- does not execute capabilities
- missing skill returns structured failure
- disabled skill returns structured failure
- over-budget skill manuals return structured failure
- owned by `pal.skill`, not `pal.behavior`

### `behavior_save`

Persist a new user-instructed or learned affordance.

Input:

- `scenario_text`
- `prompt_hint`
- optional `title`
- optional `activation_terms`
- optional `capability_refs`
- optional `skill_refs`
- optional `memory_query_hints`
- optional visibility/activation/source fields

Rules:

- allowed `source_kind`: `instructed` or `learned`
- ordinary task experience must go to memory, not affordance
- use this only when the user teaches a recurring behavior rule, or when Pal records a behavior route candidate

## Cap Search vs Behavior Advise

`tool_search` answers:

- what capabilities exist?
- what can Execution invoke now?

`behavior_advise` answers:

- what should Pal think of in this scenario?
- which skill/capability/memory hint route is behaviorally relevant?

Cap search is inventory discovery. Behavior advice is scenario routing.

Advice may return `capability_refs`, but those are pointers only. It does not become cap search and does not execute anything.

## Prompt Rules

Behavior contributes a small prompt fragment:

- use `behavior_advise` when Pal intends to act and needs route advice.
- treat returned `skill_ref` values as optional routes; call `skill_inject` only
  when the user or the selected workflow explicitly requests that manual.
- use `behavior_save` only for recurring behavior rules.
- keep affordance hints thin; multi-step procedures belong in skills.

Resident affordances may also be injected as short hints, under a strict budget.

Resident ordering must be deterministic:

- source prior
- priority
- `updated_at`
- `affordance_id`

Discoverable affordances do not enter the prompt by default.

## Plugin And Provider Integration

Plugins may declare behavior metadata with companion decorators:

```python
from pal.behavior import affordance, skill


@skill(
    skill_id="demo.commit",
    title="Commit safely",
    summary="Review and commit local changes safely.",
    manual_text="1. Inspect changes.\n2. Run tests.\n3. Commit with a clear message.",
    capability_refs=("shell",),
)
@affordance(
    affordance_id="demo.commit_when_user_asks",
    title="Commit request",
    scenario_text="The user asks Pal to commit code changes.",
    prompt_hint="Consider injecting the commit skill and checking working tree status.",
    activation_terms=("commit", "git", "changes"),
    skill_refs=("demo.commit",),
    capability_refs=("shell",),
)
class DemoProvider:
    module_id = "demo"
```

Rules:

- explicit decorators are preferred for high-quality route semantics.
- auto-generated declared affordances cover all published capabilities as a fallback.
- explicit declarations may point to skills, capabilities, and memory query hints.

## Current V1 Non-Goals

- no automatic behavior router on every user turn
- no automatic learned-affordance extraction
- no separate approval path in behavior
- no automatic memory recall from `memory_query_hints`
- no capability execution from `behavior_advise`
- no skill learning or skill storage ownership inside `behavior`

## Implementation Entry Points

- `src/pal/behavior/contracts.py`
- `src/pal/behavior/decorators.py`
- `src/pal/behavior/models.py`
- `src/pal/behavior/repository.py`
- `src/pal/behavior/service.py`
- `src/pal/behavior/tools.py`
- `src/pal/behavior/capabilities.py`
- `src/pal/behavior/prompt.py`

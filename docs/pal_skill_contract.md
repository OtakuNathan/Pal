# Pal Skill Contract

> V1 contract for skill learning, sanitization, storage, and injection.

## Concept Boundary

Skill is reusable future procedure.

It is not memory, not an affordance route, not executable permission, and not runtime policy.

```text
Memory / experience = what happened before, what Pal learned from a case
Skill = what Pal should do next time
Affordance = when Pal should think of a skill or capability
Capability = what can actually execute
```

## Skill Descriptor

Normalized skills are stored as `SkillDescriptor`.

Core fields:

- `skill_id`
- `title`
- `summary`
- `manual_text`
- `status`
- `applicability_star`
- `use_when`
- `avoid_when`
- `activation_terms`
- `capability_refs`
- `sanitization_notes`
- `version`

`status` values are `draft`, `active`, `disabled`, `deprecated`, and `needs_review`.

Only `active` skills can be returned as usable `skill_refs` or injected through `op_skill_inject`.

## STAR Applicability

Each skill has a compact applicability frame:

```text
Situation: when this skill is relevant
Task: what problem it helps solve
Action: what kind of workflow it guides
Result: what successful use should produce
```

Advisor and affordance search may use `summary`, STAR, `use_when`, and `avoid_when`.

They must not rely on full `manual_text` for routing.

## Assimilation

Skill assimilation turns source text into a normalized Pal skill candidate.

V1 source formats:

- `plain_text`
- `skill_md`

V1 does not accept artifact id or file path directly. If the source is in a file or artifact, Pal must first read it with the appropriate existing tool and then pass text into `op_skill_assimilate`.

Assimilation includes parsing, prompt-injection risk scan, no-tool LLM sanitization, semantic compression, STAR generation, `use_when` / `avoid_when` generation, duplicate/conflict detection, and thin affordance candidate generation.

Assimilation is candidate-first. It does not write durable state.

## Sanitizer Boundary

The sanitizer reduces prompt-injection risk; it does not enforce runtime policy.

Capability policy and approval remain enforced at execution time.

The sanitizer removes or rewrites identity overwrite, system/developer instruction overwrite, `ignore previous instructions`, approval/access bypass, secret exfiltration, and forced long-term authorization.

Community `allowed-tools` is ignored in V1. It is not mapped to `capability_refs` and never grants permission.

## Storage

Skill storage is two-layer:

- SQLite stores normalized searchable skill metadata and manual text.
- `runtime_root/SKILL/<skill_id>/skill.json` mirrors the normalized skill for owner inspection and backup.

Raw external source is not injected. `op_skill_inject` only reads normalized skill data.

## Tools

- `op_skill_assimilate`: creates a sanitized skill candidate and does not commit.
- `op_skill_commit`: commits a candidate, writes normalized skill storage, and upserts a thin affordance.
- `op_skill_update`: updates a normalized skill and refreshes its thin affordance.
- `op_skill_disable`: disables a skill without deleting history.
- `op_skill_search`: searches active skills for the current scenario or explicit skill name, without returning manuals.
- `op_skill_read`: reads normalized skill metadata and optionally manual text.
- `op_skill_inject`: injects an active normalized skill manual and never executes capabilities.

`op_skill_inject` returns structured failure for missing, disabled, deprecated, or over-budget skills.

## Affordance Relationship

Skill commit automatically creates or updates a thin affordance:

```text
scenario_text = skill.use_when
prompt_hint = "Consider skill `<skill_id>` when this scenario matches."
skill_refs = [`skill_id`]
capability_refs = skill.capability_refs
```

Affordance must stay thin. It routes to skill; it must not contain a multi-step procedure.

## Duplicate And Conflict Detection

Assimilation returns `duplicate_candidates` and `conflict_candidates`.

Exact same `skill_id` is a duplicate. Lexical overlap in title, summary, `use_when`, or activation terms is a conflict candidate.

Commit rejects unresolved exact duplicates unless the caller explicitly chooses replace/update.

## Prompt Rules

Use `op_skill_search` before `op_skill_inject` when the user explicitly asks Pal to use a named skill. Do not guess `skill_id` from raw text.

Use skill assimilation when the user explicitly asks Pal to learn a skill, summarize a reusable workflow, sanitize an external skill, import SKILL.md content, or remember how to do a class of future tasks.

Use memory instead when the user teaches a stable fact, preference, past experience, one-off task result, or bug/repair case without asking to turn it into procedure.

If a past experience could become a workflow, create a skill candidate first. Do not auto-commit.

## Non-Goals

- no approval UI in V1
- no automatic learned skill extraction
- no artifact/file path input for assimilation
- no `allowed-tools` support
- no direct execution from skill

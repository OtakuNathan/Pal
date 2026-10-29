# Pal Change Admission

> Goal: give Pal's human and AI contributors a minimum review bar before code enters the codebase, preventing large-volume, cross-boundary, unmaintainable changes from landing directly.

## Principle

Pal may use AI to land implementations quickly, but must not treat "being able to generate a lot of code" as design quality.

For a change to enter the mainline, it must at least answer:

- Which contract does it modify?
- Which module owner does it belong to?
- Does it cross an explicit Does Not Own boundary?
- Does it verify that the current runtime can work while alive?
- How does it roll back or get disabled when it fails?

## Change Classes

### Small Local Fix

Applicable scenarios:

- bug fix
- Single-module behavior fixes
- Documentation sync
- Small test additions

Requirements:

- List the affected files
- Include a focused test or clearly state why one is not needed
- Do not casually refactor unrelated code

### Contract Change

Applicable scenarios:

- New capability
- New provider contract
- database schema or migration
- prompt/tool surface change
- core/runtime lifecycle change

Requirements:

- First update or add the corresponding `docs/pal_*_contract.md`
- State whether Owns / Does Not Own changes
- Add an architecture or integration test
- Dogfood against a real runtime once, or explain why dogfooding is not possible

### Large Generated Change

Applicable scenarios:

- AI-generated or bulk migrations
- Spanning multiple subsystems
- New runtime manager / provider family
- Large amounts of new code

Requirements:

- Split into reviewable stages
- Each stage must be runnable, testable, and rollback-able
- Each stage states the code generation scope
- Do not mix in unrelated formatting, renaming, or directory moves
- If the diff is too large for human review, convert it first into a plan / draft / bunshin work order instead of merging directly

## Admission Checklist

Check at least the following before submitting:

- Contract: Does the change correspond to an existing contract? If not, was a new contract added?
- Ownership: Does the code stay within the correct module owner?
- Boundary: Does it violate `Does Not Own`?
- Persistence: Does it change the DB? If so, is there a migration or is incompatibility explicitly allowed?
- Runtime: Does it affect bootstrap, service loop, provider reload, or sidecar lifecycle?
- Tool Surface: Does it add or change an LLM-visible capability/tool? Are names stable and namespace-first?
- Prompt Surface: Does it change the prompt context? Does it avoid treating operational metadata as user content?
- Channel: Is interaction realization left to the provider rather than written into core/manager?
- Memory: Does it avoid treating the channel/conversation route as memory identity?
- Tests: Is there a focused test? For cross-module changes, is there an architecture/integration test?
- Dogfood: Was it verified through a real running runtime, socket client, or manager socket?
- Rollback: Can the provider/plugin/endpoint be disabled or a single commit reverted?

## AI Contributor Rule

AI contributions should be treated as a senior typist plus rapid implementer, not as a source of architectural authority.

AI may:

- implement code against an existing contract
- add tests
- keep docs in sync
- raise risks
- help break work into stages

AI may not, without an explicit contract:

- invent new ownership boundaries
- write platform-specific code into core
- bypass execution for tool execution
- treat memory as routing state
- generate large, unreviewable diffs in bulk

## Dogfood Rule

Pal runtime changes require dogfooding by default.

Verify in priority order:

- `pal run --runtime-root <runtime>`
- `pal client --runtime-root <runtime> --message "..."`
- manager socket/client (Bunshin scheduling and runtime state)
- provider reload / rescan / attach / detach
- no repeated crashes or event-loop lifecycle errors in the service log

If the real runtime cannot run, the commit message must state clearly:

- which secret/config/service is missing
- which substitute tests have already been run
- where the remaining risk lies

## Review Shape

When reviewing, look at risk first, not at lines of code.

Priority order:

1. Does it break architectural boundaries
2. Does it break the runtime lifecycle
3. Does it break data persistence or migration
4. Does it break the LLM-visible tool/capability contract
5. Is dogfooding or testing missing
6. Is there unrelated churn

Code may be large, but it must be explainable, reviewable, testable, and rollbackable.

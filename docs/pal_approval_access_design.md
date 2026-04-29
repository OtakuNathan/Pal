# Pal Approval And Access Design

> Deferred design note. Approval is intentionally planned after the skill learning/import loop is complete.

## Goal

Approval should be a lightweight safety gate, not Pal's primary interaction model.

The user experience target is:

- Pal stays fluid during normal work.
- Risky execution can still be stopped deterministically.
- Repeated prompts are avoided inside the same turn.
- Remote channels cannot silently escalate Pal into full-access mode.

## Boundary

Approval belongs to the execution/control boundary.

- `Execution` declares that a capability needs approval.
- `PalCore` owns orchestration, pending requests, grants, and channel routing.
- `Channel` renders interaction UI and returns typed interaction results.
- `Endpoint` owns endpoint-specific UX such as Telegram inline keyboards.

Execution must not import Telegram/socket endpoint code, and channels must not make authorization decisions.

## Capability Approval Gate

The preferred implementation shape is an async approval gate.

Conceptually:

```text
capability call
-> approval_required gate
-> PalCore opens approval interaction on the originating route
-> coroutine awaits user decision
-> accept: continue original call
-> reject / timeout: return structured denial
```

In Python, the suspended coroutine is the continuation. A decorator can be used as syntax sugar, but it should only build approval metadata and call a core-owned approval port.

## Static And Dynamic Context

V1 should not spend an extra LLM call to explain approval.

Approval context is deterministic:

- Static policy comes from the capability/decorator, such as title, risk level, and impact scope.
- Dynamic context comes from capability args, turn snapshot, and optional purpose fields.
- PalCore renders the approval prompt from this data.

Example approval text:

```text
Pal wants to run a shell command.

Command:
pytest tests -q

Purpose:
Verify code changes.

Impact:
Runs a local process and may read files or consume CPU.
```

The LLM may provide a `purpose` argument when calling high-risk capabilities, but approval must still work when no purpose is provided.

## Decisions

Approval decisions should support:

- `approve_once`
- `approve_for_turn`
- `deny`

`approve_for_turn` creates a short-lived grant for the current turn. It prevents repeated prompts during one coherent workflow without creating long-term privilege.

V1 grant scope:

- keyed by `turn_id`
- scoped to the originating `control_scope_key`
- tied to the capability or capability family
- optional constraints hash when the capability can provide stable constraints
- expires when the turn ends

No cross-turn "always allow" grant is planned for V1.

## Access Modes

Access mode is separate from a single approval decision.

Recommended modes:

- `limited`: default safety mode. High-risk capabilities require approval.
- `full`: owner/development mode. Soft approval gates auto-pass, but hard policy can still block.
- `locked`: read-only or chat-only mode. Write, execution, external side effects, and physical actions are blocked.

`full` is not root. Hard-deny policy remains authoritative.

Examples of hard policy:

- destructive operations outside allowed roots
- credential exfiltration
- physical-world actions marked as hard gated
- operations forbidden by runtime configuration

## Full Access Escalation

V1 should avoid passphrase challenge state machines in remote chat.

Rule:

- Local trusted endpoints, such as socket/TTY owner console, may enable `full`.
- Remote endpoints, such as Telegram, may downgrade access but must not enable `full`.

Telegram can show status and provide safe downgrade actions:

```text
Current access: full
[Switch to Limited] [Switch to Locked]
```

If the user asks Telegram to enable full access, Pal should reply that full access must be enabled from the local owner console.

This avoids:

- pending passphrase state
- secret leakage through callback payloads or logs
- race conditions around challenge expiry
- complex re-entry behavior

## Slash Commands

Planned commands:

```text
/access
/access limited
/access full
/access locked
```

Semantics:

- `/access` shows current mode and channel permissions.
- `/access limited` can be accepted from remote channels.
- `/access locked` can be accepted from remote channels.
- `/access full` is accepted only from trusted local endpoints.

Access changes apply to future turns. An active turn keeps its captured access snapshot unless interrupted and restarted.

## Telegram UX

Telegram should use the existing interaction lifecycle.

Approval interaction:

```text
[Approve once] [Always this turn] [Deny]
```

Access panel interaction:

```text
Access mode: limited
[Limited] [Locked]
[Full access requires local console]
```

Telegram callback data must remain endpoint-local. Inner layers receive typed interaction results only.

## Shell And Filesystem Policy

Shell execution is unavoidable and should be approval-gated in `limited` mode.

Filesystem operations should eventually be first-party structured capabilities, even if their internal implementation is simple. The LLM-facing API should be typed, for example:

```text
op_fs_list(path)
op_fs_read_text(path)
op_fs_write_text(path, content, overwrite)
op_fs_mkdir(path)
op_fs_move(src, dst, overwrite)
op_fs_copy(src, dst, overwrite)
op_fs_delete(path, recursive)
op_fs_stat(path)
```

The safety value comes from the structured boundary, not from whether the implementation uses Python stdlib or shell internally.

`shell.exec` remains the general escape hatch and should default to stronger approval.

## Interaction Lifecycle Reuse

Approval must reuse the generic interaction lifecycle:

- `interactive_open`
- `interactive_update`
- `interactive_resolve`
- `interactive_expire`

This keeps approval aligned with:

- control panel actions
- reset confirmation
- future skill import confirmation
- future affordance write confirmation

## Relationship To Skill Learning

Skill learning/import should be implemented first.

The skill import flow can reuse the same interaction shape in a lower-risk form:

```text
import external skill
-> sanitize and normalize
-> preview candidate
-> user accepts / edits / rejects
-> store normalized skill and generated affordance
```

This gives Pal the UX foundation needed for approval without introducing capability execution gating too early.

## Non-Goals For V1

- No LLM call solely to explain approval.
- No Telegram passphrase challenge for full access.
- No long-term "always approve" grants.
- No direct execution-to-channel dependency.
- No approval ownership inside `behavior`.
- No shell command parser as the main safety mechanism.

## Invariants

- Approval is a safety gate, not an execution engine.
- PalCore is the only orchestrator.
- Endpoint UX stays endpoint-local.
- Capability execution remains the only source of real side effects.
- `approve_for_turn` never crosses turn boundaries.
- Full access can be enabled only through trusted local control surfaces in V1.

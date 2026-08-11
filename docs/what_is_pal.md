# What Pal Is

Pal is a personal, long-running agent runtime. It is installed for one person,
runs continuously under the operating system's service manager, and keeps one
governed place for conversations, memory, tools, channels, plugins, and
delegated work.

The short version is:

> Pal handles ordinary requests directly and turns larger requests into
> durable workflows when the work needs structure, parallelism, review, or
> recovery.

This is a description of Pal's scope and design choices, not a claim that every
task needs the same machinery.

## One Runtime, Two Execution Paths

Pal does not force every request through a workflow engine.

### Direct turns

Conversation, questions, small edits, inspection, and other bounded work stay
in Pal's normal turn loop:

```text
channel -> Pal -> model/tools -> reply
```

The runtime normalizes the channel input, assembles the prompt, governs tool
use, commits conversation state, and routes the response back to the same
channel.

### Durable delegation

When a task needs longer execution, explicit contracts, multiple roles, or a
human gate, Pal can delegate it to Minion:

```text
Pal -> durable Workflow -> Architect/Reviewer -> module DAG
                                            -> Coder/Verifier
                                            -> human review or delivery
```

Minion persists workflow state independently of any single model turn or
worker process. Roles work through bounded contracts and durable checklists;
leases and fencing protect ownership; dependencies control parallel work;
triage and recovery are explicit states rather than improvised chat replies.

Delegation is not tied to source code. Families, profiles, and architecture
specializations apply the same workflow machinery to different domains; the
current built-ins include software engineering, general work, and lifestyle
workflows.

Pal remains the user-facing agent. Minion workers do not acquire their own
channels or bypass Pal when they need clarification, approval, or delivery.

## What the Runtime Owns

### A continuous personal process

Pal runs as a daemon supervised by systemd on Linux or launchd on macOS. A TTY
or messaging client may disconnect without ending Pal's work. Durable state is
loaded again after a process restart.

### Channel-neutral interaction

Setup always provisions a Unix socket as a local and recovery channel.
Optional providers, such as Telegram or a WebSocket bridge, attach to the same
runtime. Channels own transport and presentation; they do not redefine memory,
task, or execution semantics.

### Governed capabilities

Pal exposes actions through one Capability Forest. Introspection and mutation
are distinct, capabilities are discovered before invocation, and side effects
run through the execution and control boundaries instead of arbitrary module
calls.

### Durable and working memory

The memory stack separates the current working conversation, recent context,
and durable long-term recall. Compaction and persistence have explicit owners;
Minion role sessions also checkpoint the context needed to survive worker
replacement.

### Lifecycle-owned extensions

Plugins, channel providers, LSP, MCP, Minion, web providers, and their sidecars
have explicit attach/detach lifecycles. Enabled built-ins mount automatically
at startup, while an individual plugin can be refreshed without restarting the
rest of Pal.

### Proactive work

Pal can store scheduled or recurring work and deliver it into the same runtime
as a push task. Proactive execution therefore shares Pal's identity, memory,
capability, and delivery boundaries instead of becoming a disconnected cron
script with separate state.

### Human control as part of execution

Interrupts, approvals, clarification questions, workflow pause/cancel, human
review, and operator triage are real control states. They are not conventions
that a prompt merely asks the model to remember.

## Complexity Stays Behind the Boundary

Pal contains several state machines and process boundaries because it assumes
that models can return malformed output, tools can fail, processes can die,
and users can disconnect. Those mechanisms are implementation responsibilities,
not installation steps.

A normal user receives a release bundle, runs one installer, completes a guided
setup, and connects to the resulting service. Built-in plugins and the recovery
socket are provisioned automatically. Existing installations use the same
installer; upgrade migrations run without replacing the user's configuration.

See [Getting Started](getting_started.md) for the concrete path.

## Where Pal Fits

Pal is a good fit when someone wants one agent that remains available across
sessions and channels, accumulates personal context, can use governed tools,
and can delegate substantial work without making the chat transcript the only
source of truth.

The direct path remains appropriate for small tasks. Durable Minion workflows
are appropriate when the benefit of contracts, dependency-aware parallelism,
verification, and recovery exceeds their coordination cost. Pal supports both
because they solve different shapes of work.

## Current Platform Boundary

- Linux supports the full runtime and bubblewrap-sandboxed Minion workers.
- macOS supports the core runtime and LaunchAgent registration; the production
  Minion sandbox backend is not wired in this build.
- Windows is not currently supported.

These are current implementation boundaries, not properties of the runtime
model itself.

## Further Reading

- [Getting Started](getting_started.md)
- [Runtime Stack](pal_runtime_stack.md)
- [Bootstrap and Process Contract](pal_bootstrap_and_process.md)
- [Control Plane](pal_control_plane.md)
- [Minion V2 Contract Orchestration](minion_v2_contract_orchestration.md)
- [Channel Contract](pal_channel_contract.md)

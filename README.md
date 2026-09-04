<p align="center">
  <img src="docs/assets/pal-avatar.png" alt="Pal" width="140"/>
</p>

# Pal

> A personal, long-running agent runtime. One Pal, one person.

> Pal #0 — the original. Yes, that's the official face: a robot caught eating cookies. 🍪

Created and maintained by **Nathan Wu (OtakuNathan)**.

Pal is an event-driven agent runtime built for a single user. It runs as a
service on your machine, talks through local and messaging channels, remembers
what matters, and acts through a governed capability system. Ordinary requests
stay in the fast conversational path; larger jobs can be delegated to Bunshin as
durable, reviewable workflows.

The implementation is deliberately structured, but that complexity stays
inside the runtime boundary. A normal installation is one release bundle, one
installer, and one guided setup.

- [What Pal is](./docs/what_is_pal.md)
- [Install, upgrade, and connect](./docs/getting_started.md)
- [Watch or reproduce the public proof demo](./docs/public_proof_demo.md)

### Design Philosophy

Pal is designed around a few stubborn principles that show up in every layer:

- **Explicit beats implicit.** Nothing should be quietly decided for you. If a behavior can be silent, it will eventually betray you — so Pal makes trade-offs visible and reversible.
- **Compile the constraint into the structure.** Don't ask an agent to *remember* to follow a rule; encode the rule so the system physically can't violate it. Contract-first interfaces, constitutional import rules, and read-only introspection are all this principle in disguise.
- **Pay only for what you ask for.** The bare path is fast precisely because it costs nothing; every optional feature you enable (memory, verification, a bunshin) is a ledger you can inspect.
- **The executor is unreliable, so the structure must not be.** Every durable subsystem assumes the process can die, the model can ramble, and the network can lie — and checkpoints, ledgers, and gates are what make that survivable.

## Quick Start

### 1. Install a release

On Linux, install Python 3.11+ and `bubblewrap` first. On macOS, the installer
selects Homebrew Python automatically.

```bash
tar -xzf pal_v2-install-bundle.tar.gz
./install-pal.sh
```

The installer:

- selects a supported Python and creates a dedicated virtualenv;
- installs Pal and its runtime-root overlay;
- verifies the SQLite vector extension;
- creates the `pal` launcher;
- runs the five-step setup wizard for a new runtime;
- preserves existing configuration and applies migrations during an upgrade;
- runs the dependency doctor.

The default runtime root is `~/.pal`. Setup asks for identity, at least one LLM
endpoint, a channel, memory-embedding preferences, and final confirmation. It
then offers to register and start Pal as a user service.

### 2. Connect

On Linux, setup installs a systemd user service by default. On macOS it
registers a LaunchAgent. No manual `pal run` is needed after a normal install.

```bash
# Linux service health
systemctl --user status pal

# Interactive local session
pal tty --runtime-root ~/.pal

# One-shot, scriptable message
pal client --runtime-root ~/.pal --message "hello"
```

`pal run --runtime-root <dir>` still exists for development / foreground runs
and as a fallback on platforms without service registration.

### From source

Package maintainers can build the same release bundle locally:

```bash
scripts/build_package.sh
mkdir -p /tmp/pal-install
tar -xzf dist/pal_v2-install-bundle.tar.gz -C /tmp/pal-install
/tmp/pal-install/install-pal.sh
```

For editable development, use `pip install -e .`, then run `pal doctor` and
`pal setup`. To guarantee that a launcher or service executes the current
working tree instead of a packaged copy, invoke `scripts/pal_source.sh`; it
prepends `src/` to `PYTHONPATH` and accepts the same arguments as `pal`.
Set `PAL_PYTHON` when the desired interpreter is not `python3`. The setup
aliases `pal wizard` and `pal wizzard` remain available.

### Pal Debugging Pal

Pal is built to debug itself, but "attach/detach" means something specific —
two distinct loops, don't blur them:

**Disconnect / reconnect (socket connection).** The daemon and its clients are
separate processes talking over a Unix socket (`{runtime_root}/socket`).
Closing a TTY (`/exit`, `Ctrl-D`) or exiting a client script only *disconnects*
that client — the daemon, its turn queue, and its sessions keep running, and
`pal tty --runtime-root <dir>` reconnects at any time. This is a connection
lifecycle, **not** a detach.

**Detach / attach (plugin lifecycle).** Real detach is a plugin operation.
`artifact`, `behavior`, `checklist`, `proactive`, `skill`, Bunshin, LSP, MCP,
L3 providers, and web integrations can be replaced with `plugin_detach` /
`plugin_attach`. Core, execution, LLM, channel (including the recovery
socket), memory, identity, control, and failure are resident and cannot be
unloaded. Channel endpoints retain their own target-level lifecycle.

**Reboot the brain — you do it, not Pal.** Pal cannot restart itself. A
session reset is a slash command (`/reset`); a full daemon restart is
`systemctl --user restart pal` on Linux (or restarting the LaunchAgent on
macOS), performed by the operator. Because memory (L1/L2/L3), bunshin task
ledgers, and invocation checkpoints are all durable on disk, restarting the
brain costs nothing — the conversation continues with memory intact. That's
the developer loop: break it, fix it, restart, `pal tty` back in.

**Control without a chat:** slash commands (`/interrupt`, `/reset`, …) ride
the same socket as ordinary messages, so the operator can steer a live session
programmatically instead of just typing at it.

### Platform Support

- **Linux** — fully supported. Bunshin workflows run sandboxed with
  **bubblewrap (`bwrap`)**: the sandbox fails closed, so a missing or partial
  bwrap means Bunshin refuses to run rather than running unsandboxed.
- **macOS** — supported for the core runtime (service registration via
  launchd); the Bunshin sandbox backend is not wired for macOS in this build.
  **Use a Python with module-loadable SQLite** — the installer forces Homebrew
  Python for this reason: the system Python ships a static SQLite without
  `load_extension`, so `sqlite-vec` cannot load and **L3 memory silently degrades
  (incomplete memory).** Recommended: `brew install python` (the installer does
  this automatically when Homebrew is present, and installs Homebrew first if
  it is missing).
- **Windows** — not supported.

## How It Works

Pal runs as a systemd-supervised daemon. You talk to it through channels (Unix socket, Telegram, etc.). Every conversation is a **turn** — Pal normalizes your input, runs it through the LLM, executes tools if needed, and replies.

```
systemd → Pal daemon → bunshins
                ↑
         Unix socket / Telegram
```

### Architecture at a Glance

```
Foundation (async I/O, events, persistence)
  ├── Data Plane: LLM, Channel, Memory, Execution
  └── Control Plane: deterministic governance
Pal Core (thin governance center)
Governed Extensions: plugins, tasking, services
Wizard (outside runtime — provisions databases, first-run setup)
```

**PalCore** decides *what is allowed to run*. **Execution** knows *how to run it*. They never cross boundaries — modules register through PalCore, execute through Execution, and never talk to each other directly.

### Memory

Pal has three memory layers, modeled after how you actually think:

- **L1** — the complete working set of the current logical conversation or
  role session. It is runtime state; durable Bunshin sessions checkpoint and
  restore it across worker-process restarts.
- **L2** — recent context (128 items, 8 top-of-mind). Runtime-only, with hot/ghost/dormant heat states.
- **L3** — durable long-term memory. Pluggable backends (default: `sqlite-vec` with Ollama vector embeddings + FTS). This is where Pal remembers your preferences, project facts, and lessons learned.

Context compaction is split across explicit ownership boundaries: Core owns
budgeting, history-unit selection, retries, validation, and commit ordering;
Memory owns only the atomic L1 replacement plus dependent L2
cleanup/rollback; each agent host policy owns the semantic checkpoint schema
and rendering. Automatic compaction is driven by the real context budget, not
a fixed turn interval.

### Capability Forest

Everything Pal can *do* lives in a single Capability Forest — a unified tree of `introspection` and `operation` nodes. The LLM discovers capabilities by fuzzy search, then invokes them by strict canonical path.

```python
# Read-only observation
introspection.module.llm.list
introspection.endpoint.channel.inspect::telegram_main

# Side-effect actions
operation.channel.management.attach
operation.execution.exec.run
```

The surface exposed to the LLM is deliberately small: 6 singletons + 3 dynamic tools. Discovery-first — Pal searches for what it needs, then calls it.

### Tool System

Tools are the **only** execution primitive. Built-in: `shell.exec`, `tool.search`, `tool.read`. Every tool call is budgeted (max output size, timeout, read limits). Oversized results spill to artifact storage. A stagnation guard detects loops and force-terminates them.

### Prompt Assembly

PalCore gathers prompt fragments from registered providers (identity, rules, behavior, memory, etc.) and assembles them into a single system prompt. The priority hierarchy is explicit:

> **Safety/capability policy and source-of-truth, verification, and mutation rules stay active. Within those boundaries: current user instruction > active task constraints > activated skills > memory > behavior guidance > default style.**

### Turn Flow

```
Channel receives raw input
  → normalize
  → PalCore queues turn (one active turn per scope)
  → LLM processes with tools
  → reply routed back through channel
  → L1 committed
```

Control actions (`/interrupt`, `/reset`) bypass the turn queue.

### Plugin System

Plugins are how Pal grows new abilities without touching core. First-party
plugins live in `{runtime_root}/plugins/_builtin/`; community plugins in
`{runtime_root}/plugins/community/<plugin_id>/`. A plugin is a `plugin.toml`
manifest + an entrypoint exposing a side-effect-free `build_plugin`. Every
manifest declares `lifecycle_protocol = "raii.v1"`, `module_id`, and optional
plugin/port dependencies. The returned instance acquires resources in
`start(scope)` and returns a `ModuleHandle`; the host publishes ports, events,
prompts and capabilities only after start succeeds, then releases everything
in reverse order on detach. Plugins can be **hot-attached/detached** at runtime
(`plugin_rescan` / `plugin_attach` / `plugin_detach` / `plugin_enable`), no
daemon restart needed.

The resident foundation is `core`, `execution`, `llm`, `channel` (including
the socket entrypoint), `identity`, `memory`, `control`, and `failure`.
Everything else is a first-party or community plugin governed by the same
lifecycle.

**Pal ships with a built-in plugin development manual as a skill**
(`pal.plugin.development`): it covers when to prefer a plugin (optional,
detachable, hot-refreshable, domain-owned) vs. when not to (the pinned runtime
bus and shared contracts), the plugin directory layout, the
`build_plugin` / `start(scope)` contract, `ModuleHandle` lifecycle rules,
and the guardrail that `build_plugin` must never start background work, touch
hardware, mutate secrets, or do irreversible I/O. Pal consults it every time
it writes or repairs a plugin.

**OLED is the living example.** Pal's emotional display — `oled_emotion`, a
community plugin Pal wrote itself — lives in `~/.pal/plugins/community/oled_emotion/`:
a `plugin.toml` subscribing to `turn.start` / `turn.tool_call_before` /
`turn.tool_call_after` / `turn.end`, an `introspection.py` that registers the
`show_oled_emotion` tool as a resident affordance, 12 hand-made emotion GIFs,
and a `sidecar.py` + `ssd1306.py` driver that talk to the OLED over a Unix
socket — the hardware runs in a sidecar process, exactly the boundary the
development manual demands.

Built-in plugins include artifact, behavior, checklist, proactive, skill,
Bunshin, LSP, MCP, SQLite-vec L3 memory, web search (Brave + DuckDuckGo), and
stateful browser automation (pinned Playwright CLI). Enabled built-ins are
attached automatically during startup; users do not assemble the runtime by
hand.

## Bunshin: Durable Agent Workflows

Bunshin is Pal's delegation layer — the part that takes a real task and
runs it to completion as a **durable workflow**, not a chat. This is the largest
and most battle-tested subsystem in the codebase: it has executed full
end-to-end software projects (architecture review → implementation → verification)
and lifestyle planning tasks (nutrition, weekly meal/training plans) entirely
through the agent pipeline, with a human approving at the gates.

Bunshin supports multiple **task families**, each with its own architecture
templates, role profiles, and contract shapes. Current families include
`software_engineering` (Git-backed code projects with CMake/CTest verification)
and `lifestyle` (artifact-producing tasks like nutrition planning). A family
defines how the Architect decomposes work, what the Coder produces, and how the
Verifier validates it — but the durability guarantees (checkpointing, triage,
immutable ledger, human gates) are shared across all families.

### The Pipeline

A task is decomposed into **modules** (units of work) arranged in a dependency
DAG. Each module passes through distinct roles:

```
Architect → Reviewer
   ↓  (per module)
Coder (implementation) → Verifier
   ↓
Human review gate (when the contract asks for one)
```

- **Architect** settles the module-level design and writes machine-readable
  contracts *before* any code exists. For artifact families (lifestyle, etc.),
  the Architect authors the deliverable directly in an isolated role workspace.
- **Reviewer** audits the architecture before implementation is allowed to start.
- **Coder** implements one module against its contract, in an isolated worktree.
  (Not used by artifact-only families where the Architect is the sole producer.)
- **Verifier** runs the acceptance criteria against the candidate and either
  accepts it or sends it back with a structured review.

Modules with independent dependencies run **in parallel** (measured ~2.4× speedup
on a 6-module DAG); modules that depend on others queue behind their acceptances.

### What Makes It Durable

- **Contract-first, not instruction-first.** Every module gets a *contract
  snapshot* (interfaces, semantics, invariants, errors, handoffs) written by the
  Architect. The Coder reads its own contract + dependency contracts — it is not
  expected to read the whole tree. The contract, not prose, is what the Verifier
  checks against. This is "Explicit beats implicit" applied to delegation.
- **Scoped workspaces and explicit delivery.** Git-backed engineering flows use
  bounded role workspaces while running. After sink verification, Manager pins
  the accepted commit under a durable ref and emits one content-addressed
  `git format-patch` relative to the captured source snapshot. It mechanically
  replays clean-Git patches with `git am` and synthetic-snapshot patches with
  `git apply`, then compares the resulting tree
  before publication. Terminal worktrees can then retire without losing the
  authoritative commit or portable patch. Pushing a branch or opening a PR is a
  separate downstream action rather than a workflow side effect.
  Artifact-family flows (lifestyle, etc.) continue to publish their native
  content-addressed deliverables.
- **Immutable task ledger.** Each task owns an append-only `task.yaml` ledger;
  revisions are append-only too. The ledger is the source of truth for what was
  asked, what changed, and why.
- **Checkpointed coroutines.** Worker sessions checkpoint their full context
  (encrypted, fernet envelopes) after every milestone. A process crash or restart
  resumes from the last checkpoint instead of starting over — the 16-hour
  "stuck workflow" bug that stalled a run was exactly the class of failure this
  is designed to absorb.
- **Bounded model behavior.** Output length limits, stagnation guards, and
  structured reply contracts keep a talkative model from blowing up a run. When
  a model still over-produces, the triage/recover path re-dispatches with
  stricter constraints instead of silently retrying into the same wall.
- **TLA+ verified concurrency model.** The workflow state machine — architecture
  lifecycle, graph generation, replan/reuse, role assignment recovery, and
  active-lineage triage — is specified in TLA+ and checked before the
  implementation is trusted. The specs live in `spec/bunshin_v2/` and are the
  authoritative source for what states are legal, what transitions are allowed,
  and what invariants must hold. When a bug is found and fixed, the fix is
  validated against the spec first, then ported to code.
- **Observable by construction.** Workflow state, role assignments, attempts,
  durable checklists, submissions, and completed-turn timing/token metrics live
  in `bunshin.sqlite3`. Prompt/event logging remains an explicit runtime policy;
  status does not pretend that an interrupted, unsettled turn consumed zero
  work merely because no completed-turn metrics exist.

### The Human In The Loop

Bunshin is explicitly *not* autonomous-by-default. Human review gates are
first-class state (`human_review` / `human_wait`): the workflow pauses, publishes
the durable review, and waits. Nothing ships without a human approving the gate
that the task defines.

## Skill, Behavior & Proactive

Beyond the core turn loop, Pal has three "memory-adjacent" systems that shape how
it acts over time:

- **Skill** — reusable procedure manuals (playbooks) that can be searched,
  injected into a session, and learned from practice. A skill is *how to do a
  thing*; memory is *what is true*; behavior is *when to consider it*.
- **Behavior** — a condition-reflex layer: *when situation X appears, consider
  route/action Y.* Learned routing rules are recalled when their scenario
  matches, and never masquerade as facts.
- **Proactive** — scheduled, recurring, or future work driven by
  `croniter`-style definitions, delivered to Pal as push tasks rather than
  waiting for a user to say something first.

## Documentation and Deep Dives

User guides and detailed architecture contracts live in `docs/`. Start with:

- [What Pal Is](./docs/what_is_pal.md)
- [Getting Started](./docs/getting_started.md)
- [Capability Forest Structure](./docs/capability_forest_structure.md)
- [Turn Runtime Structure](./docs/turn_runtime_structure.md)
- [Documentation Index](./docs/README.md)

## Testing

```bash
python -m pytest tests/

# Single test
python -m pytest tests/test_architecture_skeleton.py::PalV2ArchitectureSkeletonTests::test_top_level_modules_import
```

Tests use `unittest` with `tempfile.mkdtemp` for isolation. No Makefile or tox.

## Database

Pal stores runtime state in SQLite via Peewee, with Bunshin owning a separate
SQLite database beneath the runtime root. A packaged upgrade runs
`pal setup --upgrade`, applies the current LLM and Bunshin schema migrations,
and preserves user configuration. `RawSQLHookRegistry` lets plugins declare
their SQL requirements without moving schema ownership into PalCore.

## Constitutional Rules

These are the lines that don't get crossed:

- **`core` does not import `.models` or `.repository` from other modules**
- **`control` does not depend on `execution.runtime`**
- **`bunshin` does not import `channel`**
- **`memory` does not import plugin implementations directly**
- **`wizard` and `bootstrap` do not expose `register_with_core`**
- **Secrets are write-only** — never returned through introspection
- **Registered slash commands are runtime-private** — matched control commands are never exposed to the LLM; unmatched `/...` text falls back to ordinary chat
- **Capability names use canonical namespace-first form** — always explicit, always stable

## License

Pal is open source under the [MIT License](./LICENSE).

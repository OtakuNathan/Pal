<p align="center">
  <img src="docs/assets/pal-avatar.png" alt="Pal" width="140"/>
</p>

# Pal

> Your personal AI companion — not a framework, not a platform. One Pal, one person.

> Pal #0 — the original. Yes, that's the official face: a robot caught eating cookies. 🍪

Pal is an event-driven agent runtime built for a single user. It runs as a daemon on your machine, talks through Unix sockets and messaging channels, remembers what matters, and acts on your behalf through a governed capability system.

### Design Philosophy

Pal is designed around a few stubborn principles that show up in every layer:

- **Explicit beats implicit.** Nothing should be quietly decided for you. If a behavior can be silent, it will eventually betray you — so Pal makes trade-offs visible and reversible.
- **Compile the constraint into the structure.** Don't ask an agent to *remember* to follow a rule; encode the rule so the system physically can't violate it. Contract-first interfaces, constitutional import rules, and read-only introspection are all this principle in disguise.
- **Pay only for what you ask for.** The bare path is fast precisely because it costs nothing; every optional feature you enable (memory, verification, a minion) is a ledger you can inspect.
- **The executor is unreliable, so the structure must not be.** Every durable subsystem assumes the process can die, the model can ramble, and the network can lie — and checkpoints, ledgers, and gates are what make that survivable.

## Quick Start

### 1. Build the package (once)

```bash
# Produces dist/pal_v2-*.whl + runtime-root overlay + dist/install-pal.sh
scripts/build_package.sh
```

### 2. Install with one command

```bash
# Everything below happens automatically:
#   - picks Python 3.11+ (Linux: PAL_PYTHON or python3; macOS: Homebrew Python)
#   - creates a dedicated virtualenv
#   - installs the wheel + runtime-root overlay
#   - verifies sqlite-vec actually loads in that Python
#   - links the `pal` launcher into ~/.local/bin
#   - runs `pal setup` (or `setup --upgrade` for an existing runtime), then `pal doctor`
./dist/install-pal.sh
```

For development instead: `pip install -e .` and run `pal doctor` → `pal setup`
(aliases: `pal wizard` / `pal wizzard`) manually.

### 3. Run

The daemon is registered as a service by the wizard's **last step** — `pal setup`
prompts for service registration (and `install-pal.sh` runs it for you): on
Linux it installs a systemd **user** service (default `pal.service`) and starts
it with `systemctl --user enable --now`; on macOS it registers a LaunchAgent.
No manual `pal run` needed — the daemon is already up.

```bash
# Verify the daemon is running (Linux systemd)
systemctl --user status pal

# Open an interactive TTY attached to the running instance
# (Prompt Toolkit input, Rich Markdown output; /exit, /quit or Ctrl-D to leave)
pal tty --runtime-root /path/to/runtime

# Send a single message to the running instance (scriptable, one-shot)
pal client --runtime-root /path/to/runtime --message "hello"
```

`pal run --runtime-root <dir>` still exists for development / foreground runs
(and as the fallback on platforms without service registration) — you just
don't need it after a normal install.

### Pal Debugging Pal

Pal is built to debug itself, but "attach/detach" means something specific —
two distinct loops, don't blur them:

**Disconnect / reconnect (socket connection).** The daemon and its clients are
separate processes talking over a Unix socket (`{runtime_root}/socket`).
Closing a TTY (`/exit`, `Ctrl-D`) or exiting a client script only *disconnects*
that client — the daemon, its turn queue, and its sessions keep running, and
`pal tty --runtime-root <dir>` reconnects at any time. This is a connection
lifecycle, **not** a detach.

**Detach / attach (plugin lifecycle).** Real detach is a plugin/module
operation: a subsystem such as the Telegram channel can be taken out of the
running daemon (`plugin_detach`) and put back in (`plugin_attach`) without
restarting anything else. That's how you hot-replace or partially restart a
broken component — fix a channel plugin, re-attach it, and the rest of the
daemon never notices.

**Reboot the brain — you do it, not Pal.** Pal cannot restart itself. A
session reset is a slash command (`/reset`); a full daemon restart is
`systemctl --user restart pal` on Linux (or restarting the LaunchAgent on
macOS), performed by the operator. Because memory (L1/L2/L3), minion task
ledgers, and invocation checkpoints are all durable on disk, restarting the
brain costs nothing — the conversation continues with memory intact. That's
the developer loop: break it, fix it, restart, `pal tty` back in.

**Control without a chat:** slash commands (`/interrupt`, `/reset`, …) ride
the same socket as ordinary messages, so the operator can steer a live session
programmatically instead of just typing at it.

### Platform Support

- **Linux** — fully supported. Minion workflows run sandboxed with
  **bubblewrap (`bwrap`)**: the sandbox fails closed, so a missing or partial
  bwrap means Minion refuses to run rather than running unsandboxed.
- **macOS** — supported for the core runtime (service registration via
  launchd); the Minion sandbox backend is not wired for macOS in this build.
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
systemd → Pal daemon → minions
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
  role session. It is runtime state; durable Minion sessions checkpoint and
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

> **User's explicit instructions > behavior advice > affordances. Operating Rules and capability policy are always active.**

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
manifest + an entrypoint exposing `build_plugin`, which registers
capabilities, tools, affordances, prompt fragments, or turn-event handlers
through a `ModuleHandle` — and can be **hot-attached/detached** at runtime
(`plugin_rescan` / `plugin_attach` / `plugin_detach` / `plugin_enable`), no
daemon restart needed.

**Pal ships with a built-in plugin development manual as a skill**
(`pal.plugin.development`): it covers when to prefer a plugin (optional,
detachable, hot-refreshable, domain-owned) vs. when not to (runtime bus,
shared contracts, control plane), the plugin directory layout, the
`build_plugin` dependency-injection contract, `ModuleHandle` lifecycle rules,
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

Built-in plugins: SQLite-vec L3 memory, web search (Brave + DuckDuckGo), web
fetch (Playwright + HTTP).

## Minion: Durable Agent Workflows

Minion is Pal's delegation layer — the part that takes a real engineering task and
runs it to completion as a **durable workflow**, not a chat. This is the largest
and most battle-tested subsystem in the codebase: it has executed full
end-to-end software projects (architecture review → implementation → verification)
entirely through the agent pipeline, with a human approving at the gates.

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
  contracts *before* any code exists.
- **Reviewer** audits the architecture before implementation is allowed to start.
- **Coder** implements one module against its contract, in an isolated worktree.
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
- **Isolated worktrees.** Every task runs in its own `git worktree`, so
  concurrent tasks never pollute each other, and every change is diffable against
  the source repo.
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
- **Observable by construction.** Every LLM round is recorded in
  `minion.sqlite3` (input/cached/uncached tokens, latency, tool calls), and every
  invocation snapshot is replayable. The cost of any run is auditable down to a
  single round — no black boxes.

### The Human In The Loop

Minion is explicitly *not* autonomous-by-default. Human review gates are
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

## Architecture Deep Dives

Detailed design docs (in Chinese) live in `docs/`. Key reads:

- [Capability Forest Structure](./docs/capability_forest_structure.md)
- [Turn Runtime Structure](./docs/turn_runtime_structure.md)
- [Architecture Overview](./docs/README.md)

## Testing

```bash
python -m pytest tests/

# Single test
python -m pytest tests/test_architecture_skeleton.py::PalV2ArchitectureSkeletonTests::test_top_level_modules_import
```

Tests use `unittest` with `tempfile.mkdtemp` for isolation. No Makefile or tox.

## Database

SQLite via Peewee. The runtime expects a pre-migrated database — schema migration is external. `RawSQLHookRegistry` lets plugins register SQL (e.g., FTS tables) without auto-executing.

## Constitutional Rules

These are the lines that don't get crossed:

- **`core` does not import `.models` or `.repository` from other modules**
- **`control` does not depend on `execution.runtime`**
- **`minion` does not import `channel`**
- **`memory` does not import plugin implementations directly**
- **`wizard` and `bootstrap` do not expose `register_with_core`**
- **Secrets are write-only** — never returned through introspection
- **Registered slash commands are runtime-private** — matched control commands are never exposed to the LLM; unmatched `/...` text falls back to ordinary chat
- **Capability names use canonical namespace-first form** — always explicit, always stable

## License

Pal is currently source-available with all rights reserved unless a separate license is granted; see [LICENSE](./LICENSE).

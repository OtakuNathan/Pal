# Pal

> Your personal AI companion — not a framework, not a platform. One Pal, one person.

Pal is an event-driven agent runtime built for a single user. It runs as a daemon on your machine, talks through Unix sockets and messaging channels, remembers what matters, and acts on your behalf through a governed capability system.

## Quick Start

```bash
pip install -e .

# Launch the runtime (daemon mode, logs → pal.log)
pal run --runtime-root /path/to/runtime

# Send a message
pal client --runtime-root /path/to/runtime --message "hello"

# Invoke a capability directly
pal tool-call --runtime-root /path/to/runtime --name <name> --args '<json>'
```

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

- **L1** — the current conversation. Fleeting, gone when the turn ends.
- **L2** — recent context (128 items, 8 top-of-mind). Runtime-only, with hot/ghost/dormant heat states.
- **L3** — durable long-term memory. Pluggable backends (default: `sqlite-vec` with Ollama vector embeddings + FTS). This is where Pal remembers your preferences, project facts, and lessons learned.

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

First-party plugins live in `{runtime_root}/plugins/_builtin/`. Community plugins in `{runtime_root}/plugins/community/`. Plugins register capabilities, subscribe to turn events, and extend Pal without touching core.

Built-in plugins: SQLite-vec L3 memory, web search (Brave + DuckDuckGo), web fetch (Playwright + HTTP).

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
- **Slash commands are runtime-private** — never exposed to the LLM
- **Capability names use canonical namespace-first form** — always explicit, always stable

## License

Pal is currently source-available with all rights reserved unless a separate license is granted; see [LICENSE](./LICENSE).

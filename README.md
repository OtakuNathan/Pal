<p align="center">
  <img src="docs/assets/pal-avatar.png" alt="Pal" width="140"/>
</p>

# Pal

> A personal, long-running agent runtime. One Pal, one person.

Created and maintained by **Nathan Wu (OtakuNathan)**.

Pal runs as a service on your machine, stays available across channels, remembers
what matters, and acts through governed tools. Everyday requests stay in the
conversation; larger jobs can become durable Bunshin workflows with independent
verification and explicit delivery.

## Capabilities

### Conversation across channels

Talk through a local terminal, Telegram, a WebSocket bridge, or a connected
desktop avatar. Channels share the same Pal, memory and governance. Deterministic
controls let you interrupt work, reset context, review proposals and select models.

### Memory and continuity

Working history, recent context and pluggable long-term memory serve different
purposes. Compaction preserves continuity, encrypted recovery restores working
state after a restart, and searchable archives retain retired dialogue.

### Tools and integrations

Pal can read and edit files, run commands, search the web, operate a stateful
browser, inspect code through LSP, and use external MCP tools. Capabilities have
explicit contracts and are discovered from the live registry. Optional extensions
add shell sessions, remote execution and device-specific integrations.

### Durable delegation with Bunshin

Bunshin handles substantial work through persistent tasks, isolated workspaces,
independent verification and recoverable execution. Planned mode uses reviewed
contracts and a dependency graph; direct mode sends a settled repository task to
a Coder/Verifier pair. Families support software engineering, general work and
artifact-producing tasks such as lifestyle planning.

### Skills and scheduled work

Skills provide reusable procedure manuals. Proactive tasks support reminders,
recurring checks and scheduled work through Pal's normal governance. Results can
return to a configured channel or use an explicit silent destination.

### Extensions and self-maintenance

Plugins contribute capabilities, providers, presentation and sidecars through
explicit lifecycles. Pal can inspect its own runtime, diagnose failures and carry
out authorized maintenance. Plugin refresh and host restart have separate owners.

## Design Philosophy

- **Make decisions explicit.** Configuration, permissions, task contracts and
  lifecycle operations should be visible and inspectable.
- **Put constraints in the structure.** Typed interfaces, ownership boundaries,
  admission rules and independent verification enforce rules beyond the prompt.
- **Match the machinery to the work.** Small requests use direct turns; durable
  delegation adds contracts, parallelism and review when the task benefits.
- **Design for interruption.** Models, tools, processes and networks can fail.
  Checkpoints, receipts and recovery states make those boundaries manageable.
- **Keep one accountable agent.** Channels and workers extend Pal's reach;
  user interaction and governance remain with Pal.

## Get Started

A release includes an installer and guided setup. Linux supports the full runtime
and sandboxed Bunshin workers; macOS supports the core runtime and LaunchAgent
registration. Windows is not currently supported.

[Install, configure and connect](docs/getting_started.md) ·
[What Pal is](docs/what_is_pal.md) ·
[Documentation index](docs/README.md)

## Explore the Documentation

| Topic | Guide |
| --- | --- |
| Runtime architecture and ownership | [Runtime stack](docs/pal_runtime_stack.md) |
| Tools, discovery and execution | [Tool surface](docs/pal_tool_surface.md) |
| Delegation and verified delivery | [Bunshin workflows](docs/bunshin_contract_orchestration.md) · [Direct mode](docs/bunshin_direct_mode.md) |
| Memory and recovery | [Memory contract](docs/pal_memory_contract.md) |
| Channels and interaction | [Channel contract](docs/pal_channel_contract.md) |
| Models, transports and hooks | [LLM contract](docs/pal_llm_contract.md) |
| Plugins and packages | [Package installation](docs/pal_package_installation.md) |
| Development and validation | [Developer guide](docs/development.md) · [Formal checks](spec/README.md) |

## License

Pal is open source under the [MIT License](LICENSE).

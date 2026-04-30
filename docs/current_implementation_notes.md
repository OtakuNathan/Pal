# Current Implementation Notes

This file is a short sync point for the current codebase when older design notes lag behind implementation.

## Prompt Assembly

Current system block order:

```text
Identity -> System Surfaces -> Operating Rules -> Behavior Routing -> Memory Routing -> Skill Learning -> Resident Affordances -> Memory Context -> Runtime Overlay
```

Ownership:

- Identity comes from the identity provider.
- System Surfaces and Operating Rules come from core prompt fragments.
- Behavior Routing and Resident Affordances come from behavior providers.
- Memory Routing and memory context projection come from memory providers.
- Skill Learning comes from skill providers.

`System Surfaces` is a real top-level system section. It is not embedded inside Operating Rules.

## Memory Projection

The current memory prompt no longer exposes one generic `Working Memory` block.

Current memory-context blocks:

- `Recent Context`
- `Current Summary`
- `Remembered Facts`
- `Relevant Experience`
- `Behavior Guidance`

If memory has been recalled or is present in the prompt, Pal is instructed to treat it as reference context before decisions or actions. This is a prompt-level governance rule; runtime still cannot inspect the model's private reasoning.

## Artifacts

Current-turn artifacts are driven by `event.payload["artifact_refs"]`.

Rules:

- Explicit current artifact refs are authoritative.
- Empty current text with no current artifact refs does not expose historical hot artifacts.
- Historical hot artifact fallback requires explicit artifact/file/image/audio language.
- Weak deictic references such as "this" or "that" do not trigger historical hot artifact exposure.
- URL text is ignored when checking historical artifact relevance.
- LLM-visible artifact metadata hides source secrets.
- `local_file.preferred_path` may be shown only as safe metadata for tools that explicitly accept local paths.

## Tool Surface

The direct LLM tool surface is configured by `src/pal/core/tool_surface.toml`.

Resident artifact tools are intentionally limited to:

- `op_artifact_info`
- `op_artifact_read`

Other artifact tools remain discoverable.

MCP-projected tools and prompt render capabilities are not resident by default.

## MCP

MCP is a first-party detachable plugin backed by a manager sidecar.

The sidecar owns:

- config discovery under `runtime_root/plugins/mcp`
- stdio process/session lifecycle
- initialize and `notifications/initialized`
- tools/prompts pagination
- tool calls and prompt rendering
- discovery snapshots

The Pal plugin owns:

- starting/stopping the sidecar
- refreshing projection
- publishing compiled capabilities
- registering declared MCP prompt skills

Current defaults:

- MCP server request timeout: 300 seconds
- Pal-to-manager IPC timeout: 300 seconds

## Capability Boundary

Capability availability is runtime state.

Persisted metadata may explain prior state, but live facts such as attached status, process status, socket status, and capability availability must be inspected live.

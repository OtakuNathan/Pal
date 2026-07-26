# Pal Tool Surface

`ToolSurface` controls which registered capabilities are exposed to the LLM as direct function-calling tools.

Pal may register many capabilities, but only a small resident set should enter the LLM tool window. Everything else remains in the execution inventory and is discoverable through capability search.

## Source Of Truth

The resident surface is data-driven:

- Config file: `src/pal/core/tool_surface.toml`
- Runtime selector: `src/pal/core/tool_surface.py`

Changing the direct LLM tool set should normally be a TOML edit, not a Python code change.

At runtime, `/refresh_tool_surface` reloads this TOML into the active PalCore instance for future turns.

## Current Resident Surface

As of the current implementation, resident tools are intentionally small:

### Execution

- `tool_read`
- `tool_search`
- `shell`
- `file_read`
- `file_edit`
- `file_write`
- `file_state`
- `tool_call`

These keep the model able to inspect and invoke any registered capability without making every capability resident, while giving common UTF-8 file work a structured path.

`shell` is the command/test/build/script escape hatch. It should not be the default path for ad-hoc `find`, `grep`, `cat`, `head`, `tail`, `sed`, `awk`, `echo`, heredoc file edits, or Pal runtime state/config inspection when a dedicated capability exists.

### Discovery Schema

`tool_search` is the resident discovery entry point. Its primary argument is
`query`, not `name`.

Important arguments:

- `query`: natural-language search text or a partial capability name
- `namespace`: `intro`/`introspection` for inspection capabilities, or
  `op`/`operation` for mutating/external-service actions
- `family`: optional family filter
- `module_id`: optional module filter
- `tags`: optional tag filters
- `top_k` / `limit`: compact hit count
- `facets`: defaults to false; when true, include namespace/module/family counts
  for broad-search narrowing

The default result should stay compact and return top hits only. Facets are
available when the model needs narrowing statistics, but they should not be
returned by default.

### Behavior

- `behavior_advise`
- `behavior_save`

Behavior advice is resident routing. Skill is intentionally not resident; skill tools remain discoverable and invocable through execution discovery when the user explicitly asks to learn/use a reusable procedure, or when behavior advice returns a skill ref.

### Artifact

- `artifact_info`
- `artifact_read`

Only metadata inspection and text-like reads are resident. Artifact list/search/select/content-search/transcribe remain discoverable when needed.

### Web

- `web_search`
- `web_read`

### Dynamic Memory Provider Tools

The active durable memory provider is resolved at runtime:

- `memory_recall`
- `memory_write`
- `memory_update`
- `memory_delete`

These stay resident because recall, commit, correction, and explicit deletion are frequent global workflows.

## Non-Resident But Discoverable

Examples:

- `artifact_list`
- `artifact_search`
- `artifact_select`
- `artifact_grep`
- `artifact_transcribe`
- `channel_send_attachment`
- `channel_send_message`
- `skill_assimilate`
- `skill_commit`
- `skill_update`
- `skill_disable`
- `skill_search`
- `skill_read`
- `skill_inject`
- MCP-projected tools such as `op_mcp_<server>_tool_<tool>`
- MCP prompt render capabilities such as `op_mcp_<server>_prompt_<prompt>_render`

The model should find these through `tool_search` and invoke them through `tool_call`. Use `shell` only for actual shell commands, not as a generic capability invocation or discovery substitute.

## Artifact Tool Boundary

Artifact tools accept `artifact_id`, not arbitrary local paths.

`artifact_grep` searches existing text-like representations only. It does not inspect image pixels, perform OCR, or create audio transcripts. If an artifact needs OCR, ASR, PDF parsing, or image processing, Pal must discover a suitable capability for that representation or path.

## MCP Tool Boundary

MCP tools are not resident by default. The MCP manager plugin compiles discovered server tools into Pal-native capabilities and publishes them into the capability inventory.

MCP prompt templates become declared skills plus render capabilities. They do not enter the resident prompt automatically.

## Invariants

- Resident tools are only the small high-frequency surface.
- All non-resident capabilities remain discoverable through execution discovery.
- Dynamic provider tools must resolve the live active provider at runtime.
- Tool exposure is not capability availability. Availability is runtime state and must be inspected when it matters.
- External protocol surfaces such as MCP must not bypass Pal execution, approval, or capability policy.

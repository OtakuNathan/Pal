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

- `op_exec_disc_read`
- `op_exec_disc_search`
- `op_exec_run`
- `op_exec_capability_call`

These keep the model able to inspect and invoke any registered capability without making every capability resident.

### Behavior

- `op_behavior_advise`
- `op_behavior_affordance_submit`

Behavior advice is resident routing. Skill is intentionally not resident; skill tools remain discoverable and invocable through execution discovery when the user explicitly asks to learn/use a reusable procedure, or when behavior advice returns a skill ref.

### Artifact

- `op_artifact_info`
- `op_artifact_read`

Only metadata inspection and text-like reads are resident. Artifact list/search/select/content-search/transcribe remain discoverable when needed.

### Web

- `op_web_search_query`
- `op_web_fetch_read`

### Dynamic Memory Provider Tools

The active L3 provider is resolved at runtime:

- `op_l3_recall_query`
- `op_l3_commit_write`
- `op_l3_correct_patch`

These stay resident because recall, commit, and correction are frequent global workflows.

## Non-Resident But Discoverable

Examples:

- `op_artifact_list`
- `op_artifact_search`
- `op_artifact_select`
- `op_artifact_content_search`
- `op_artifact_transcribe`
- `op_channel_send_attachment`
- `op_skill_assimilate`
- `op_skill_commit`
- `op_skill_update`
- `op_skill_disable`
- `op_skill_search`
- `op_skill_read`
- `op_skill_inject`
- MCP-projected tools such as `op_mcp_<server>_tool_<tool>`
- MCP prompt render capabilities such as `op_mcp_<server>_prompt_<prompt>_render`

The model should find these through `op_exec_disc_search` and invoke them through `op_exec_capability_call` or `op_exec_run`.

## Artifact Tool Boundary

Artifact tools accept `artifact_id`, not arbitrary local paths.

`op_artifact_content_search` searches existing text-like representations only. It does not inspect image pixels, perform OCR, or create audio transcripts. If an artifact needs OCR, ASR, PDF parsing, or image processing, Pal must discover a suitable capability for that representation or path.

## MCP Tool Boundary

MCP tools are not resident by default. The MCP manager plugin compiles discovered server tools into Pal-native capabilities and publishes them into the capability inventory.

MCP prompt templates become declared skills plus render capabilities. They do not enter the resident prompt automatically.

## Invariants

- Resident tools are only the small high-frequency surface.
- All non-resident capabilities remain discoverable through execution discovery.
- Dynamic provider tools must resolve the live active provider at runtime.
- Tool exposure is not capability availability. Availability is runtime state and must be inspected when it matters.
- External protocol surfaces such as MCP must not bypass Pal execution, approval, or capability policy.

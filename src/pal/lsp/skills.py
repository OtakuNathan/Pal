from __future__ import annotations

from pal.skill.contracts import SkillApplicabilitySTAR, SkillDescriptor


PAL_LSP_TEMPLATE_DEVELOPMENT_SKILL_ID = "pal.lsp.template.development"


PAL_LSP_TEMPLATE_DEVELOPMENT_MANUAL = """# Pal LSP Template Development

Use this skill when Pal needs to add, review, repair, or explain support for a new language server or LSP server template.

## Boundary

LSP server templates are runtime configuration, not core code. Prefer creating or updating a runtime template under:

```text
<runtime_root>/plugins/lsp/servers/<server_id>.toml
```

Do not edit package builtin templates under `src/pal/lsp/server_templates/` unless the user is intentionally upstreaming a first-party default. Runtime templates override builtin templates with the same `server_id`, matching the LSP manager's normal discovery behavior.

## Runtime Template Shape

Create a small TOML file with this shape:

```toml
server_id = "example_ls"
display_name = "Example Language Server"
command = ["example-language-server"]
args = ["--stdio"]
extensions = [".example"]
language_ids = ["example"]
workspace_markers = ["example.toml", ".git"]
install_hint = "Install example-language-server and ensure it is on PATH."
startup_timeout_ms = 30000
diagnostics_timeout_ms = 10000
```

Fields:

- `server_id`: stable unique id. Use lowercase, underscores, or hyphens.
- `command`: argv list, not a shell string.
- `args`: argv list. Include stdio flags when the server needs them.
- `extensions`: file extensions including the dot.
- `language_ids`: canonical ids used by LSP discovery and language metadata.
- `workspace_markers`: files/directories that identify a workspace root.
- `install_hint`: concrete install guidance without secrets.
- timeouts: raise startup/diagnostics timeouts for npx, JVM, or slow indexers.

For npm-distributed servers, prefer the npx sidecar route:

```toml
command = ["npx"]
args = ["--yes", "--package", "some-language-server", "some-language-server", "--stdio"]
startup_timeout_ms = 60000
diagnostics_timeout_ms = 10000
```

Pal sidecars already prepare PATH for common Node/nvm installs. Do not shell-wrap npx.

## Workflow

1. Inspect existing templates with `op_lsp_status`, source, or file reads.
2. Choose the canonical `language_id` the LSP template should advertise.
3. Write the LSP runtime template to `<runtime_root>/plugins/lsp/servers/<server_id>.toml`.
4. Validate the LSP template with `pal.lsp.config.load_lsp_server_file` or an isolated unit test.
5. Run LSP rescan through `op_lsp_mgmt_rescan` when the running Pal runtime should see the new LSP template.
6. Verify with `op_lsp_status`, `op_lsp_doctor`, and a representative diagnostics/hover request when a sample file exists.

## Safety

- Do not install language servers unless the user approved dependency installation.
- Do not write secrets, credentials, or user-specific absolute paths into templates.
- Do not overwrite an existing runtime template without comparing it first.
- If a binary is missing, leave a clear `install_hint` and report the missing dependency; missing binaries should be visible as LSP health, not crash Pal.

## Verification Checklist

Before calling the language support done:

1. Runtime template file exists at `<runtime_root>/plugins/lsp/servers/<server_id>.toml`.
2. `load_lsp_server_file` parses it and preserves command, args, extensions, language_ids, markers, and timeouts.
3. `op_lsp_mgmt_rescan` succeeds when the user wants the running runtime refreshed.
4. `op_lsp_doctor` reports ok, missing_binary, disabled, or another structured status instead of failing unstructured.
5. The handoff lists the template path, server id, language id, any code changes, tests run, and any dependency still missing.
"""


def lsp_declared_skills(*, module_id: str = "lsp") -> tuple[SkillDescriptor, ...]:
    return (
        SkillDescriptor(
            skill_id=PAL_LSP_TEMPLATE_DEVELOPMENT_SKILL_ID,
            module_id=module_id,
            title="Pal LSP Template Development",
            summary="Develop runtime-root LSP server templates for programming languages.",
            manual_text=PAL_LSP_TEMPLATE_DEVELOPMENT_MANUAL,
            activation_terms=(
                "lsp template",
                "language server template",
                "new language lsp",
                "add language support",
                "lsp server",
                "plugins/lsp/servers",
                "language_ids",
                "op_lsp_mgmt_rescan",
                "language server",
            ),
            capability_refs=(
                "op_lsp_mgmt_rescan",
                "op_lsp_status",
                "op_lsp_doctor",
                "op_lsp_diagnostics",
            ),
            applicability_star=SkillApplicabilitySTAR(
                situation="Pal needs to add or repair LSP support for a programming language.",
                task="Create or update a runtime-root LSP server template.",
                action="Use the template shape, runtime config path, parser check, rescan, and verification checklist.",
                result="The language is visible to LSP discovery without changing core code.",
            ),
            use_when=(
                "Use when the user asks Pal to add a language server, support a new language in LSP, "
                "or create an LSP server template."
            ),
            avoid_when=(
                "Avoid for ordinary plugin development, LLM adapters, channel providers, Minion workspace setup, "
                "or one-off project files that do not add reusable LSP support."
            ),
            source_format="internal_skill",
            source_refs=(
                "pal.lsp.skills",
                "src/pal/lsp/config.py",
                "docs/pal_engineering_quality_gates.md",
            ),
            metadata={
                "internal": True,
                "runtime_root_layout": "plugins/lsp/servers/<server_id>.toml",
                "may_require_code_changes": True,
            },
        ),
    )

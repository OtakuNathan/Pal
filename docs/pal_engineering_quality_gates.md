# Pal Engineering Quality Gates

This note records the LSP, shell, reviewer, and verifier design direction discussed for Pal V1 hardening.

It is a design baseline, not an implementation status claim. Implementation should keep the changes small, data-driven, and aligned with the existing provider/manager/ledger architecture.

## Goal

Pal's engineering workflow should improve by adding harder truth sources and stronger review gates, not by giving coder bunshins more unchecked freedom.

The target direction:

- Give reviewers and verifiers more source-of-truth tools.
- Make reviewers participate at every important development node: plan, code, checkpoint, repair, and acceptance.
- Let the manager own scheduling, ledger facts, and gate decisions.
- Prevent shell from becoming an invisible editor.
- Keep LLM-facing prompts small and role-scoped; do not show a bunshin work it should not execute.

## Reviewer Position

The reviewer is not a final afterthought. It should become a gatekeeper for both planning and implementation.

Recommended lifecycle:

```text
plan_created
  -> plan_review_pending
  -> plan_review_approved
  -> plan_accepted
  -> coder_claimed_checkpoint
  -> verification_pending
  -> verified
  -> milestone_closed
```

The semantic split matters:

- Coder checkpoint means "the coder claims the milestone is complete."
- Reviewer approval means "an independent agent found enough evidence."
- Manager acceptance means "the milestone cursor can advance."

The manager should not accept a milestone only because a runner produced final text. It should accept from ledger evidence.

## LSP Role

LSP should be treated as a read-only code-fact tool, not a code-writing tool.

Useful roles:

- Pal core fallback when it needs live code facts.
- Planner source inspection before writing a plan.
- Coder symbol lookup before editing.
- Reviewer/verifier API existence checks, reference checks, diagnostics, and impact analysis.

LSP should not provide v1 write operations such as rename, code actions, formatting, completion, or apply-workspace-edit.

V1 operations:

```text
lsp_status
lsp_doctor
lsp_hover
lsp_definition
lsp_implementation
lsp_references
lsp_prepare_call_hierarchy
lsp_incoming_calls
lsp_outgoing_calls
lsp_document_symbols
lsp_workspace_symbols
lsp_diagnostics
```

Every LSP result used as evidence should carry enough metadata to judge freshness:

```json
{
  "evidence_id": "...",
  "server_id": "clangd",
  "method": "textDocument/definition",
  "workspace_root": "...",
  "file": "...",
  "file_sha256": "...",
  "position": {"line": 12, "character": 8},
  "timestamp": "...",
  "result": []
}
```

LSP is not an absolute truth source. It proves how the active language server resolved the current workspace under its current configuration. For C/C++ this depends heavily on `compile_commands.json`, include paths, macros, SDK roots, and `.clangd`.

## LSP Provider Shape

Prefer a first-party LSP provider plugin with a Node sidecar.

Reasoning:

- LSP client libraries are strongest in the VS Code/Node ecosystem.
- A sidecar keeps Pal core independent from Node details.
- Pal already has the provider/plugin lifecycle shape needed for this.
- The provider can manage language server lifecycle while Pal owns capability exposure, evidence, permissions, and ledger integration.

Suggested layering:

```text
Pal core / execution
  -> LspProvider facade
  -> first-party lsp-provider plugin
  -> Node sidecar
  -> vscode-jsonrpc / vscode-languageserver-protocol
  -> clangd / pyright / typescript-language-server / ...
```

The first-party plugin can directly enter the Pal loop. Third-party LSP providers should go through explicit trust and enablement.

Do not expose raw LSP requests to the model in v1. Fixed read-only operations are easier to permission, audit, and use as evidence.

## Language Server Templates

The provider should support many useful languages through data-driven server templates, not core-specific if/else branches.

Templates to include:

```text
C / C++ / ObjC / ObjC++       clangd
Python                        pyright or pylsp
TypeScript / JavaScript       typescript-language-server
Rust                          rust-analyzer
Go                            gopls
Java                          jdtls
C#                            csharp-ls or OmniSharp
Lua                           lua-language-server
Shell                         bash-language-server
JSON / JSONC                  vscode-json-language-server
YAML                          yaml-language-server
HTML                          vscode-html-language-server
CSS / SCSS / LESS             vscode-css-language-server
```

These should be configured as templates, not bundled as required dependencies. Missing binaries should produce `missing_binary` or `disabled`, not runtime failure.

Example template shape:

```toml
id = "clangd"
display_name = "clangd"
command = "clangd"
args = ["--background-index"]
extensions = [".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".m", ".mm"]
language_ids = ["c", "cpp", "objective-c", "objective-cpp"]
workspace_markers = ["compile_commands.json", ".clangd", ".git"]
install_hint = "Install clangd and provide compile_commands.json for best C/C++ results."
```

`lsp_doctor` should check:

- binary availability
- workspace root detection
- required project config such as `compile_commands.json`
- server initialize success
- sample `didOpen`
- diagnostics availability
- hover/definition sanity where possible

## LSP Pitfalls

Known pitfalls to handle explicitly:

- LSP evidence can be stale after file edits.
- Diagnostics are asynchronous and may be pending, fresh, stale, timed out, or unavailable.
- Position encoding is 0-based and often UTF-16; this matters for non-ASCII text.
- Monorepos may have multiple roots and multiple build directories.
- C/C++ headers may not map cleanly to a translation unit.
- Servers crash, hang, or index for a long time.
- Different language servers support different subsets of LSP.
- Evidence expires when file content, workspace root, server config, or SDK config changes.

V1 can use a simple freshness strategy: before every request, compare file hash with the sidecar's open-file state; if changed, reopen or resync the file.

## Shell Boundary

Do not attempt to perfectly parse shell. Shell is too expressive for reliable static safety analysis.

The runtime rule is:

```text
Shell can run commands, tests, builds, and diagnostics.
The bwrap projection, not command parsing, limits readable and writable paths.
```

Pal separates OS authority from workflow correctness:

- The workspace is read-only by default. Coder receives declared product implementation scopes plus `tests/<module_name>/developer` writable; Module Verifier receives only `tests/<module_name>/verification` writable. Each role reads the other durable corpus without modifying it.
- Immutable inputs, verifier overlays, and Git metadata remain read-only.
- Shell and structured file tools see the same sandbox paths; there is no command-name trap or second path namespace.
- Submit handlers validate the final changed-path set, candidate ownership, and required evidence before a state transition.

This is closer to the Codex-style boundary:

```text
Use structured tools when their semantics reduce mistakes.
Use shell when it is the natural execution interface.
Constrain both through bwrap.
Verify final state through tests, diff, reviewer, and ledger.
```

It is different from trying to prove every shell command is read-only before running it.

## Role-Specific Shell Policy

Planner:

- Prefer no shell, or only tightly scoped read/inspection commands.
- Use file read/search/LSP/source inspection.
- Do not mutate workspace.

Coder:

- May run shell for tests, builds, status, and diagnostics.
- Must write code through `file_edit` / `file_write`.
- Must create milestone checkpoint through `checkpoint_commit`.
- Shell-based git mutation should remain disallowed; checkpoint commit is the structured path.

Reviewer/verifier:

- May run tests/builds/lints and inspection commands.
- Must not mutate the coder workspace.
- May create temporary tests or probes only in `/tmp`, `$TMPDIR`, or an isolated verifier workspace.
- Must report commands run, observed output, and verdict.

## Evidence Requirements

Plan review should check:

- claimed APIs exist or are explicitly marked as assumptions
- referenced modules/files actually exist
- proposed milestones are executable
- topology/module ordering is valid
- test strategy matches risk

Code review should check:

- code quality and behavioral regressions
- implementation matches plan and milestone contract
- test evidence exists and is fresh
- APIs are verified through source/docs/LSP/build evidence
- shell did not create unexplained workspace mutations

For API existence claims, one evidence source is often not enough. Prefer at least one of:

- source inspection
- official documentation
- LSP definition/hover/references evidence
- compile/build/test evidence

For high-risk code, reviewer/verifier should rerun or independently reproduce key checks.

## Manager Responsibilities

The manager is the scheduler and ledger authority.

It should:

- own active work order state
- feed one milestone at a time to the runner
- record checkpoint and review evidence
- decide whether the next milestone can be issued
- reject or block advancement on missing evidence
- detect unauthorized workspace mutation where practical
- preserve enough event and artifact refs for later resume

The runner should not see work it should not execute. If the current turn is only milestone 3, the bunshin prompt should not invite it to execute milestone 4.

## Comparison Notes

Claude Code:

- Has dedicated FileRead/FileEdit/FileWrite tools.
- Has a complex Bash read-only classifier and permission flow.
- Uses LSP as a read-only code intelligence tool.
- Uses verifier/subagent workflows to independently check non-trivial work.

Codex-style boundary:

- Relies on sandbox, approvals, and structured editing.
- Uses patch/diff as the primary edit path.
- Does not try to prove arbitrary shell is safe.

Pal should combine the useful pieces:

- structured file edit/write contracts
- manager ledger and checkpoint commits
- reviewer/verifier gates
- LSP as code-fact evidence
- workspace mutation audit instead of full shell parsing

## Implementation Slices

Keep implementation incremental.

Recommended order:

1. Document and profile hardening: make reviewer/verifier authority explicit.
2. Manager ledger gate: distinguish coder checkpoint, reviewer verified, and manager accepted.
3. Shell mutation audit: detect workspace changes that lack structured evidence.
4. Plan review gate: reviewer validates plan artifacts before plan acceptance.
5. Code review gate: reviewer/verifier validates checkpoint before milestone closure.
6. First-party LSP provider plugin with read-only fixed operations.
7. LSP evidence integration into plan/code review.

Do not start by building a complete IDE-like LSP layer or a complete shell parser.

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, create_model

from pal.execution.tool_facade import OpaqueToolOutput, StrictToolModel


def _strict_model(name: str, fields: dict[str, tuple[Any, Any]]):
    if not fields and name.endswith("Output"):
        return OpaqueToolOutput
    return create_model(name, __base__=StrictToolModel, **fields)


ArtifactCapabilitiesArtifactIntrospectionProviderListInput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderListInput',
    {
        'query_context': (str, Field(None)),
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderListOutput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderListOutput',
    {
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderInfoInput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderInfoInput',
    {
        'artifact_id': (str, Field(..., description='Artifact id from Available Artifacts or artifact_search.')),
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderInfoOutput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderInfoOutput',
    {
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderReadInput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderReadInput',
    {
        'artifact_id': (str, Field(..., description='Artifact id from Available Artifacts or artifact_search.')),
        'representation': (Literal['auto', 'text', 'page_text', 'chunk_text', 'transcript', 'metadata'], Field('auto', description='Text-like representation only. Do not use this to inspect visual image pixels.')),
        'page': (int, Field(None)),
        'chunk': (int, Field(None)),
        'max_chars': (int, Field(12000)),
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderReadOutput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderReadOutput',
    {
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderSearchInput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderSearchInput',
    {
        'query': (str, Field(None)),
        'kind': (str, Field(None)),
        'time_hint': (str, Field('recent')),
        'limit': (int, Field(5)),
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderSearchOutput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderSearchOutput',
    {
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderSelectInput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderSelectInput',
    {
        'artifact_id': (str, Field(..., description='Artifact id from Available Artifacts or artifact_search.')),
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderSelectOutput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderSelectOutput',
    {
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderGrepInput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderGrepInput',
    {
        'artifact_id': (str, Field(..., description='Artifact id from Available Artifacts or artifact_search.')),
        'query': (str, Field(...)),
        'top_k': (int, Field(5)),
        'max_chars_per_result': (int, Field(2000)),
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderGrepOutput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderGrepOutput',
    {
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderTranscribeInput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderTranscribeInput',
    {
        'artifact_id': (str, Field(..., description='Artifact id from Available Artifacts or artifact_search.')),
    },
)

ArtifactCapabilitiesArtifactIntrospectionProviderTranscribeOutput = _strict_model(
    'ArtifactCapabilitiesArtifactIntrospectionProviderTranscribeOutput',
    {
    },
)

BehaviorCapabilitiesBehaviorIntrospectionProviderAdviseInput = _strict_model(
    'BehaviorCapabilitiesBehaviorIntrospectionProviderAdviseInput',
    {
        'scenario': (str, Field(..., description='Current situation Pal is facing; include the routing uncertainty or risky decision point.')),
        'intent': (str, Field(None, description='Optional intended outcome.')),
        'turn_kind': (str, Field(None, description='Turn type, such as chat, service, or minion.')),
        'constraints': (list[str], Field(None)),
        'already_considered': (list[str], Field(None)),
        'top_k': (int, Field(5, ge=0)),
    },
)

BehaviorCapabilitiesBehaviorIntrospectionProviderAdviseOutput = _strict_model(
    'BehaviorCapabilitiesBehaviorIntrospectionProviderAdviseOutput',
    {
        'candidates': (list[dict[str, Any]], Field(None)),
        'fallback_used': (bool, Field(None)),
        'router_error': (str, Field(None)),
    },
)

BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceSubmitInput = _strict_model(
    'BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceSubmitInput',
    {
        'scenario_text': (str, Field(..., description='Scenario that should activate this affordance.')),
        'prompt_hint': (str, Field(..., description='Short behavioral hint body Pal should remember. Do not repeat the title as a prefix.')),
        'title': (str, Field(None, description='Optional short label for this behavior guidance.')),
        'activation_terms': (list[str], Field(None, description='Optional concrete terms that help match this scenario later.')),
        'capability_refs': (list[str], Field(None, description='Optional exact tool/capability names this behavior may route toward.')),
        'skill_refs': (list[str], Field(None, description='Optional skill ids that may provide reference manuals for this scenario.')),
        'memory_query_hints': (list[str], Field(None, description='Optional recall_memory query hints for facts/cases relevant to this behavior.')),
        'conflict_resolution': (Literal['ask', 'merge', 'overwrite', 'skip'], Field('ask', description='What to do when the same scenario already has behavior guidance. Use ask by default so Pal asks the user whether to merge, overwrite, or leave it unchanged.')),
        'resident': (bool, Field(False, description="Set true only for behavior guidance that should be always visible in Pal's prompt. Leave false for normal guidance that the behavior router recalls when the scenario matches.")),
    },
)

BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceSubmitOutput = _strict_model(
    'BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceSubmitOutput',
    {
        'affordance_id': (str, Field(None)),
        'learn_result': (Literal['learned', 'merged', 'overwritten', 'skipped'], Field(None)),
        'source_kind': (str, Field(None)),
        'scenario_text': (str, Field(None)),
        'prompt_hint': (str, Field(None)),
        'reason': (str, Field(None)),
        'action_required': (str, Field(None)),
        'conflict_resolution_options': (list[str], Field(None)),
        'candidates': (list[dict[str, Any]], Field(None)),
    },
)

BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceUpdateInput = _strict_model(
    'BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceUpdateInput',
    {
        'affordance': (str, Field(..., description='Original behavior guidance text to match. Pass the affordance text itself; Pal resolves the internal record.')),
        'affordance_id': (str, Field(None, description='Legacy exact affordance_id. Prefer affordance text instead.')),
        'scenario_text': (str, Field(None, description='Updated activation scenario text. Do not use this when replacing the visible behavior guidance shown in <behavior_guidance>; use prompt_hint for that.')),
        'prompt_hint': (str, Field(None, description='Updated visible behavior guidance body rendered in <behavior_guidance>. Use this when the user asks to replace, edit, or update the guidance/original text. Do not repeat the title as a prefix.')),
        'title': (str, Field(None)),
        'activation_terms': (list[str], Field(None)),
        'capability_refs': (list[str], Field(None)),
        'skill_refs': (list[str], Field(None)),
        'memory_query_hints': (list[str], Field(None)),
        'resident': (bool, Field(None, description="Set true to make this guidance always visible in Pal's prompt, or false to keep it behavior-router recalled.")),
    },
)

BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceUpdateOutput = _strict_model(
    'BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceUpdateOutput',
    {
        'affordance_id': (str, Field(None)),
        'affordance_hash': (str, Field(None)),
        'updated_fields': (list[str], Field(None)),
    },
)

BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceDeleteInput = _strict_model(
    'BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceDeleteInput',
    {
        'affordance': (str, Field(..., description='Original behavior guidance text to match. Pass the affordance text itself; Pal resolves the internal record.')),
        'affordance_id': (str, Field(None, description='Legacy exact affordance_id. Prefer affordance text instead.')),
    },
)

BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceDeleteOutput = _strict_model(
    'BehaviorCapabilitiesBehaviorIntrospectionProviderAffordanceDeleteOutput',
    {
        'affordance_id': (str, Field(None)),
        'affordance_hash': (str, Field(None)),
        'deleted': (bool, Field(None)),
    },
)

ChannelCapabilitiesChannelIntrospectionProviderSendAttachmentInput = _strict_model(
    'ChannelCapabilitiesChannelIntrospectionProviderSendAttachmentInput',
    {
        'path': (str, Field(..., description='Local filesystem path to the file to send.')),
        'caption': (str, Field(None, description='Optional caption to send with the attachment.')),
        'file_name': (str, Field(None, description='Optional display filename.')),
        'mime_type': (str, Field(None, description='Optional MIME type hint.')),
    },
)

ChannelCapabilitiesChannelIntrospectionProviderSendAttachmentOutput = _strict_model(
    'ChannelCapabilitiesChannelIntrospectionProviderSendAttachmentOutput',
    {
        'attachment_id': (str, Field(None)),
        'path': (str, Field(None)),
        'file_name': (str, Field(None)),
        'mime_type': (str, Field(None)),
        'reason': (str, Field(None)),
    },
)

ChannelCapabilitiesChannelIntrospectionProviderEnableInput = _strict_model(
    'ChannelCapabilitiesChannelIntrospectionProviderEnableInput',
    {
        'target_id': (str, Field(...)),
    },
)

ChannelCapabilitiesChannelIntrospectionProviderDisableInput = _strict_model(
    'ChannelCapabilitiesChannelIntrospectionProviderDisableInput',
    {
        'target_id': (str, Field(...)),
    },
)

ChannelCapabilitiesChannelIntrospectionProviderAttachInput = _strict_model(
    'ChannelCapabilitiesChannelIntrospectionProviderAttachInput',
    {
        'target_id': (str, Field(...)),
    },
)

ChannelCapabilitiesChannelIntrospectionProviderDetachInput = _strict_model(
    'ChannelCapabilitiesChannelIntrospectionProviderDetachInput',
    {
        'target_id': (str, Field(...)),
    },
)

ChannelCapabilitiesChannelIntrospectionProviderRescanInput = _strict_model(
    'ChannelCapabilitiesChannelIntrospectionProviderRescanInput',
    {
        'attach_enabled_endpoints': (bool, Field(None, description='Whether to attach enabled endpoints after provider discovery.')),
    },
)

ChannelCapabilitiesChannelIntrospectionProviderReloadProviderInput = _strict_model(
    'ChannelCapabilitiesChannelIntrospectionProviderReloadProviderInput',
    {
        'target_id': (str, Field(...)),
    },
)

ChannelCapabilitiesChannelIntrospectionProviderSetAuthMaterialInput = _strict_model(
    'ChannelCapabilitiesChannelIntrospectionProviderSetAuthMaterialInput',
    {
        'material': (dict[str, Any], Field(..., description='Provider-specific auth credentials (key-value pairs)')),
    },
)

CoreCapabilitiesCoreIntrospectionProviderConfigureInput = _strict_model(
    'CoreCapabilitiesCoreIntrospectionProviderConfigureInput',
    {
        'mode': (str, Field(None)),
    },
)

ExecutionFileCapabilitiesFileCapabilityMixinReadInput = _strict_model(
    'ExecutionFileCapabilitiesFileCapabilityMixinReadInput',
    {
        'file_path': (str, Field(..., description='Path to the file to read.')),
        'offset': (int, Field(None, description='1-based line number to start reading from. Defaults to 1.', ge=1)),
        'limit': (int, Field(None, description='Maximum number of lines to return. Defaults to 2000.', ge=1)),
    },
)

ExecutionFileCapabilitiesFileCapabilityMixinReadOutput = _strict_model(
    'ExecutionFileCapabilitiesFileCapabilityMixinReadOutput',
    {
        'file_path': (str, Field(None)),
        'content': (str, Field(None)),
        'start_line': (int, Field(None)),
        'end_line': (int, Field(None)),
        'total_lines': (int, Field(None)),
        'truncated': (bool, Field(None)),
        'full_view': (bool, Field(None)),
        'unchanged': (bool, Field(None)),
        'encoding': (str, Field(None)),
        'error_code': (str, Field(None)),
    },
)

ExecutionFileCapabilitiesFileCapabilityMixinEditInput = _strict_model(
    'ExecutionFileCapabilitiesFileCapabilityMixinEditInput',
    {
        'file_path': (str, Field(..., description='Path to the file to edit.')),
        'old_string': (str, Field(..., description='Exact text to find and replace.')),
        'new_string': (str, Field(..., description='Replacement text.')),
        'replace_all': (bool, Field(False, description='Replace every exact occurrence. Leave false to require one unique match.')),
    },
)

ExecutionFileCapabilitiesFileCapabilityMixinEditOutput = _strict_model(
    'ExecutionFileCapabilitiesFileCapabilityMixinEditOutput',
    {
        'file_path': (str, Field(None)),
        'error_code': (str, Field(None)),
        'patch': (str, Field(None)),
        'match_count': (int, Field(None)),
    },
)

ExecutionFileCapabilitiesFileCapabilityMixinWriteInput = _strict_model(
    'ExecutionFileCapabilitiesFileCapabilityMixinWriteInput',
    {
        'file_path': (str, Field(..., description='Path to create or overwrite.')),
        'content': (str, Field(..., description='Complete UTF-8 text content for the file.')),
    },
)

ExecutionFileCapabilitiesFileCapabilityMixinWriteOutput = _strict_model(
    'ExecutionFileCapabilitiesFileCapabilityMixinWriteOutput',
    {
        'file_path': (str, Field(None)),
        'bytes_written': (int, Field(None)),
        'created': (bool, Field(None)),
        'operation': (Literal['create', 'update'], Field(None)),
        'patch': (str, Field(None)),
        'encoding': (str, Field(None)),
        'error_code': (str, Field(None)),
    },
)

ExecutionFileCapabilitiesFileCapabilityMixinDeleteInput = _strict_model(
    'ExecutionFileCapabilitiesFileCapabilityMixinDeleteInput',
    {
        'file_path': (str, Field(..., description='Path to delete.')),
        'expected_sha256': (str, Field(None, description='Optional current SHA-256 digest for regular files. If supplied, a prior file_read snapshot is not required.')),
        'recursive': (bool, Field(False, description='Required for directory deletion. Regular file deletion does not require this.')),
    },
)

ExecutionFileCapabilitiesFileCapabilityMixinDeleteOutput = _strict_model(
    'ExecutionFileCapabilitiesFileCapabilityMixinDeleteOutput',
    {
        'file_path': (str, Field(None)),
        'deleted': (bool, Field(None)),
        'path_kind': (str, Field(None)),
        'recursive': (bool, Field(None)),
        'sha256': (str, Field(None)),
        'error_code': (str, Field(None)),
    },
)

ExecutionFileCapabilitiesFileCapabilityMixinStateInput = _strict_model(
    'ExecutionFileCapabilitiesFileCapabilityMixinStateInput',
    {
        'file_path': (str, Field(None, description='Optional file path to check against the read-before-edit cache.')),
    },
)

ExecutionFileCapabilitiesFileCapabilityMixinStateOutput = _strict_model(
    'ExecutionFileCapabilitiesFileCapabilityMixinStateOutput',
    {
        'cached_file_count': (int, Field(None)),
        'file_path': (str, Field(None)),
        'cached': (bool, Field(None)),
        'valid': (bool, Field(None)),
        'full_view': (bool, Field(None)),
        'content_length': (int, Field(None)),
    },
)

ExecutionGitCapabilitiesGitCapabilityMixinGitInput = _strict_model(
    'ExecutionGitCapabilitiesGitCapabilityMixinGitInput',
    {
        'cmd': (str, Field(..., description='Git command without shell syntax, for example `status --short`, `diff -- src/app.py`, `log --oneline -5`, `restore -- path.py`, or `revert --no-commit HEAD`. An optional leading `git` is accepted.')),
        'cwd': (str, Field(None, description='Optional repository working directory.')),
        'timeout_ms': (int, Field(None, description='Optional timeout in milliseconds.', ge=1)),
    },
)

ExecutionGitCapabilitiesGitCapabilityMixinGitOutput = _strict_model(
    'ExecutionGitCapabilitiesGitCapabilityMixinGitOutput',
    {
        'cmd': (str, Field(None)),
        'tokens': (list[str], Field(None)),
        'cwd': (str, Field(None)),
        'classification': (dict[str, Any], Field(None)),
        'returncode': (int, Field(None)),
        'stdout': (str, Field(None)),
        'stderr': (str, Field(None)),
        'stdout_truncated': (bool, Field(None)),
        'stderr_truncated': (bool, Field(None)),
        'timeout_ms': (int, Field(None)),
        'changed_files': (list[str], Field(None)),
        'audit_id': (str, Field(None)),
        'before_head': (str, Field(None)),
        'after_head': (str, Field(None)),
        'before_status': (str, Field(None)),
        'after_status': (str, Field(None)),
        'diff_stat': (str, Field(None)),
        'undo_hint': (str, Field(None)),
        'error_code': (str, Field(None)),
    },
)

ExecutionShellExecShellExecCapabilityMixinShellInput = _strict_model(
    'ExecutionShellExecShellExecCapabilityMixinShellInput',
    {
        'cmd': (str, Field(..., description='Shell command to execute. Use only for command execution, tests, builds, scripts, process probes, and package commands. Use bounded `tree -a -L 3 --filelimit 200 --noreport` listings (or `find -maxdepth 3 -print | head -n 500` when tree is unavailable). If visible, prefer search for text search, read_file for file reads, edit_file for edits, write_file for writes, delete_path for deletion, and git for git status/diff/log/show. Avoid cat/head/tail/grep/rg/sed/awk/tee/echo/printf redirection/rm/unlink/rmdir/git rm/find -delete for repo file operations when the matching capability is visible. For Pal runtime/module/minion/capability state or actions, use built-in Pal tools before shell. In minion workspaces, do not run git add/commit/reset/checkout/clean/merge/rebase/push for checkpointing; use the dedicated checkpoint commit capability instead.')),
        'cwd': (str, Field(None, description='Optional working directory.')),
        'timeout_ms': (int, Field(None, description='Optional timeout in milliseconds.', ge=1)),
    },
)

ExecutionShellExecShellExecCapabilityMixinShellOutput = _strict_model(
    'ExecutionShellExecShellExecCapabilityMixinShellOutput',
    {
        'cmd': (str, Field(None)),
        'cwd': (str, Field(None)),
        'returncode': (int, Field(None)),
        'stdout': (str, Field(None)),
        'stderr': (str, Field(None)),
        'stdout_truncated': (bool, Field(None)),
        'stderr_truncated': (bool, Field(None)),
        'timeout_ms': (int, Field(None)),
        'display_text': (str, Field(None)),
    },
)

ExecutionToolSearchExecutionDiscoveryCapabilityMixinCapabilityCallInput = _strict_model(
    'ExecutionToolSearchExecutionDiscoveryCapabilityMixinCapabilityCallInput',
    {
        'name': (str, Field(..., description='Exact indirect alias. Use search_tools/read_tool first when unsure.')),
        'args': (dict[str, Any], Field(None, description="Arguments matching that capability's schema from read_tool.")),
    },
)

ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput = _strict_model(
    'ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput',
    {
        'query': (str, Field(None, description="Natural-language search text or partial capability name, for example 'llm endpoint config' or 'send attachment'.")),
        'namespace': (Literal['inspect', 'action', 'introspection', 'operation'], Field(None, description='Capability namespace. Use inspect to inspect state; use action to perform work.')),
        'family': (str, Field(None, description='Optional family filter such as management, lifecycle, endpoint, or search.')),
        'module_id': (str, Field(None, description='Optional module filter such as llm, memory, channel, artifact, minion, or web_search.')),
        'tags': (list[str], Field(None, description='Optional tags that every result must include.')),
        'top_k': (int, Field(None, description='Maximum number of compact hits to return.', ge=1)),
        'limit': (int, Field(None, description='Alias for top_k.', ge=1)),
        'facets': (bool, Field(None, description='Default false. Set true to include namespace/module/family counts for broad-search narrowing.')),
    },
)

ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchOutputHitsItem = _strict_model(
    'ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchOutputHitsItem',
    {
        'alias': (str, Field(...)),
        'search_text': (str, Field(...)),
        'invocation_mode': (Literal['direct', 'indirect'], Field(...)),
        'input_shape': (dict[str, Any], Field(...)),
        'namespace': (str, Field(...)),
        'family': (str, Field(...)),
        'module_id': (str, Field(...)),
        'tags': (list[str], Field(...)),
        'score': (int, Field(...)),
    },
)

ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchOutput = _strict_model(
    'ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchOutput',
    {
        'hits': (list[ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchOutputHitsItem], Field(None)),
        'total_count': (int, Field(None)),
        'returned_count': (int, Field(None)),
        'top_k': (int, Field(None)),
        'truncated': (bool, Field(None)),
        'applied_filters': (dict[str, Any], Field(None)),
        'facets': (dict[str, Any], Field(None, description='Only present when requested with facets=true; counts deduplicated candidates.')),
        'usage_hint': (str, Field(None, description='Only present for broad facet responses that need narrowing guidance.')),
    },
)

ExecutionToolSearchExecutionDiscoveryCapabilityMixinReadInput = _strict_model(
    'ExecutionToolSearchExecutionDiscoveryCapabilityMixinReadInput',
    {
        'name': (str, Field(...)),
    },
)

ExecutionToolSearchExecutionDiscoveryCapabilityMixinReadOutput = _strict_model(
    'ExecutionToolSearchExecutionDiscoveryCapabilityMixinReadOutput',
    {
        'alias': (str, Field(...)),
        'invocation_mode': (Literal['direct', 'indirect'], Field(...)),
        'description': (str, Field(...)),
        'example': (dict[str, Any] | None, Field(None)),
        'input_schema': (dict[str, Any], Field(...)),
        'output_schema': (dict[str, Any], Field(...)),
    },
)

ExecutionToolSearchExecutionDiscoveryCapabilityMixinResultPageInput = _strict_model(
    'ExecutionToolSearchExecutionDiscoveryCapabilityMixinResultPageInput',
    {
        'result_ref': (str, Field(..., description='The result_ref shown in a prior tool result; this is the original tool_call_id.')),
        'page': (int, Field(None, description="1-based page number. With anchor='head', page=1 is the first page. With anchor='tail', page=1 is the last page and page=2 is second-to-last.", ge=1)),
        'anchor': (Literal['head', 'tail'], Field(None, description="Read from the start ('head') or end ('tail') of the paged result. Defaults to head.")),
        'tail': (bool, Field(None, description="Shorthand for anchor='tail'. Useful for log-like output.")),
        'page_size': (int, Field(None, description='Optional character page size.', ge=256)),
    },
)

ExecutionToolSearchExecutionDiscoveryCapabilityMixinResultPageOutput = _strict_model(
    'ExecutionToolSearchExecutionDiscoveryCapabilityMixinResultPageOutput',
    {
        'result_ref': (str, Field(None)),
        'page': (int, Field(None)),
        'page_count': (int, Field(None)),
        'has_more': (bool, Field(None)),
        'has_more_before': (bool, Field(None)),
        'has_more_after': (bool, Field(None)),
        'anchor': (str, Field(None)),
        'anchor_page': (int, Field(None)),
        'start_offset': (int, Field(None)),
        'end_offset': (int, Field(None)),
        'original_size': (int, Field(None)),
        'page_size': (int, Field(None)),
        'page_text': (str, Field(None)),
    },
)

LlmCapabilitiesLLMIntrospectionProviderShowInput = _strict_model(
    'LlmCapabilitiesLLMIntrospectionProviderShowInput',
    {
        'model_id': (str, Field(...)),
    },
)

LlmCapabilitiesLLMIntrospectionProviderSetActiveEndpointInput = _strict_model(
    'LlmCapabilitiesLLMIntrospectionProviderSetActiveEndpointInput',
    {
        'active_endpoint_id': (str, Field(...)),
    },
)

LspPluginLspManagerPluginProviderPrepareWorkspaceInput = _strict_model(
    'LspPluginLspManagerPluginProviderPrepareWorkspaceInput',
    {
        'workspace_root': (str, Field(..., description='Canonical project/worktree root to prepare for later LSP queries.')),
        'primary_language': (str, Field(None, description='Optional primary language; omit to detect it from workspace source files.')),
        'languages': (list[str], Field(None, description='Optional additional workspace languages.')),
        'compile_commands_path': (str, Field(None, description='Optional existing compile_commands.json path for C/C++/Objective-C.')),
        'include_paths': (list[str], Field(None, description='Existing project include directories, relative to workspace_root or absolute.')),
        'stub_include_paths': (list[str], Field(None, description='Existing caller-created SDK/stub include directories; LSP never fabricates these APIs.')),
        'cpp_standard': (str, Field(None, description='Optional C/C++ language standard such as c++17.')),
        'lsp_compile_flags': (list[str], Field(None, description='Optional fallback compile flags when no project compile database exists.')),
        'prewarm': (bool, Field(True, description='Initialize matching language servers immediately after preparing the environment.')),
    },
)

LspPluginLspManagerPluginProviderDoctorInput = _strict_model(
    'LspPluginLspManagerPluginProviderDoctorInput',
    {
        'file': (str, Field(None)),
        'path': (str, Field(None)),
        'workspace_root': (str, Field(None)),
        'server_id': (str, Field(None)),
    },
)

LspPluginLspManagerPluginProviderDiagnosticsInput = _strict_model(
    'LspPluginLspManagerPluginProviderDiagnosticsInput',
    {
        'file': (str, Field(...)),
        'workspace_root': (str, Field(None)),
        'server_id': (str, Field(None)),
    },
)

LspPluginLspManagerPluginProviderHoverInput = _strict_model(
    'LspPluginLspManagerPluginProviderHoverInput',
    {
        'file': (str, Field(...)),
        'workspace_root': (str, Field(None)),
        'server_id': (str, Field(None)),
        'line': (int, Field(..., description='0-based line number')),
        'character': (int, Field(..., description='0-based UTF-16 character offset')),
    },
)

LspPluginLspManagerPluginProviderDefinitionInput = _strict_model(
    'LspPluginLspManagerPluginProviderDefinitionInput',
    {
        'file': (str, Field(...)),
        'workspace_root': (str, Field(None)),
        'server_id': (str, Field(None)),
        'line': (int, Field(..., description='0-based line number')),
        'character': (int, Field(..., description='0-based UTF-16 character offset')),
    },
)

LspPluginLspManagerPluginProviderImplementationInput = _strict_model(
    'LspPluginLspManagerPluginProviderImplementationInput',
    {
        'file': (str, Field(...)),
        'workspace_root': (str, Field(None)),
        'server_id': (str, Field(None)),
        'line': (int, Field(..., description='0-based line number')),
        'character': (int, Field(..., description='0-based UTF-16 character offset')),
    },
)

LspPluginLspManagerPluginProviderReferencesInput = _strict_model(
    'LspPluginLspManagerPluginProviderReferencesInput',
    {
        'file': (str, Field(...)),
        'workspace_root': (str, Field(None)),
        'server_id': (str, Field(None)),
        'line': (int, Field(..., description='0-based line number')),
        'character': (int, Field(..., description='0-based UTF-16 character offset')),
        'include_declaration': (bool, Field(True)),
    },
)

LspPluginLspManagerPluginProviderPrepareCallHierarchyInput = _strict_model(
    'LspPluginLspManagerPluginProviderPrepareCallHierarchyInput',
    {
        'file': (str, Field(...)),
        'workspace_root': (str, Field(None)),
        'server_id': (str, Field(None)),
        'line': (int, Field(..., description='0-based line number')),
        'character': (int, Field(..., description='0-based UTF-16 character offset')),
    },
)

LspPluginLspManagerPluginProviderIncomingCallsInput = _strict_model(
    'LspPluginLspManagerPluginProviderIncomingCallsInput',
    {
        'file': (str, Field(...)),
        'workspace_root': (str, Field(None)),
        'server_id': (str, Field(None)),
        'line': (int, Field(..., description='0-based line number')),
        'character': (int, Field(..., description='0-based UTF-16 character offset')),
    },
)

LspPluginLspManagerPluginProviderOutgoingCallsInput = _strict_model(
    'LspPluginLspManagerPluginProviderOutgoingCallsInput',
    {
        'file': (str, Field(...)),
        'workspace_root': (str, Field(None)),
        'server_id': (str, Field(None)),
        'line': (int, Field(..., description='0-based line number')),
        'character': (int, Field(..., description='0-based UTF-16 character offset')),
    },
)

LspPluginLspManagerPluginProviderDocumentSymbolsInput = _strict_model(
    'LspPluginLspManagerPluginProviderDocumentSymbolsInput',
    {
        'file': (str, Field(...)),
        'workspace_root': (str, Field(None)),
        'server_id': (str, Field(None)),
    },
)

LspPluginLspManagerPluginProviderWorkspaceSymbolsInput = _strict_model(
    'LspPluginLspManagerPluginProviderWorkspaceSymbolsInput',
    {
        'query': (str, Field(...)),
        'workspace_root': (str, Field(None)),
        'server_id': (str, Field(None)),
    },
)

McpPluginMcpManagerPluginProviderReadInput = _strict_model(
    'McpPluginMcpManagerPluginProviderReadInput',
    {
        'server_id': (str, Field(...)),
    },
)

McpPluginMcpManagerPluginProviderAttachInput = _strict_model(
    'McpPluginMcpManagerPluginProviderAttachInput',
    {
        'server_id': (str, Field(...)),
    },
)

McpPluginMcpManagerPluginProviderDetachInput = _strict_model(
    'McpPluginMcpManagerPluginProviderDetachInput',
    {
        'server_id': (str, Field(...)),
    },
)

McpPluginMcpManagerPluginProviderImagePrepareInput = _strict_model(
    'McpPluginMcpManagerPluginProviderImagePrepareInput',
    {
        'artifact_id': (str, Field(None)),
        'path': (str, Field(None)),
        'url': (str, Field(None)),
        'mode': (Literal['auto', 'url', 'path', 'base64', 'data_url'], Field(None)),
    },
)

MemoryCapabilitiesMemoryIntrospectionProviderRecallInput = _strict_model(
    'MemoryCapabilitiesMemoryIntrospectionProviderRecallInput',
    {
        'queries': (list[str], Field(None, description='One to three focused natural-language search strings for the remembered fact, preference, project context, prior decision, repair lesson, failure case, or candidate memory. Include concrete names, modules, error text, symptoms, failed fixes, or user terms when known. Do not paste large raw context; summarize the lookup target.')),
        'topic_scope': (list[str], Field(None, description='Optional short topic keywords that narrow retrieval, such as a project, subsystem, user preference area, or failure domain. This is semantic narrowing, not the storage scope; do not use system/task here.')),
        'task_id': (str, Field(None, description='Optional exact task, work order, run, or minion task identifier from current context. When provided, recall is narrowed to task-scoped memories for that task. Do not invent or guess task ids.')),
        'limit': (int, Field(None, description='Maximum memories to return. Use 3-5 by default; use a larger value only when comparing several possible matches.', ge=1, le=10)),
        'kind': (Literal['fact', 'case'], Field(None, description='Optional memory type filter. Use fact for stable facts, preferences, project context, or prior decisions. Use case for prior failures, debugging attempts, repair lessons, task experience, or when current work hits an error and prior pitfall/fix experience may exist.')),
        'view': (Literal['summary', 'origin'], Field(None, description='Use summary by default for normal work. Use origin only when provenance, source text, or extra detail is needed to resolve a conflict, update/delete a memory safely, or audit where the memory came from.')),
    },
)

MemoryCapabilitiesMemoryIntrospectionProviderWriteInputStar = _strict_model(
    'MemoryCapabilitiesMemoryIntrospectionProviderWriteInputStar',
    {
        'situation': (str, Field(..., description='The situation or failure context that future Pal should recognize.')),
        'task': (str, Field(..., description='The task or objective Pal was trying to complete in that situation.')),
        'action': (str, Field(..., description='The action, repair, or decision that mattered.')),
        'result': (str, Field(..., description='The outcome, lesson, or observed result that makes the case reusable.')),
    },
)

MemoryCapabilitiesMemoryIntrospectionProviderWriteInput = _strict_model(
    'MemoryCapabilitiesMemoryIntrospectionProviderWriteInput',
    {
        'kind': (Literal['fact', 'case'], Field(..., description='Use fact for stable facts, preferences, project context, or decisions. Use case for reusable task/failure/repair lessons; case requires star.')),
        'summary': (str, Field(..., description='Concise prompt-ready memory text future Pal can read directly.')),
        'search_text': (str, Field(..., description='Retrieval/source text with concrete names, symptoms, decisions, or wording. This can be longer than summary but should not be raw unrelated context.')),
        'topics': (list[str], Field(None, description='Optional short semantic topic tags such as project, subsystem, preference area, or failure domain.')),
        'task_id': (str, Field(None, description='Optional exact task, work order, run, or minion task id from current context. Providing it binds this memory to that task scope. Do not invent task ids.')),
        'star': (MemoryCapabilitiesMemoryIntrospectionProviderWriteInputStar, Field(None, description="Required when kind='case'; omit for fact memories. STAR case detail for reusable failures, repairs, or task lessons.")),
    },
)

MemoryCapabilitiesMemoryIntrospectionProviderUpdateInputStar = _strict_model(
    'MemoryCapabilitiesMemoryIntrospectionProviderUpdateInputStar',
    {
        'situation': (str, Field(..., description='The situation or failure context that future Pal should recognize.')),
        'task': (str, Field(..., description='The task or objective Pal was trying to complete in that situation.')),
        'action': (str, Field(..., description='The action, repair, or decision that mattered.')),
        'result': (str, Field(..., description='The outcome, lesson, or observed result that makes the case reusable.')),
    },
)

MemoryCapabilitiesMemoryIntrospectionProviderUpdateInput = _strict_model(
    'MemoryCapabilitiesMemoryIntrospectionProviderUpdateInput',
    {
        'mem_ref': (str, Field(..., description='Opaque memory ref returned by recall_memory. Copy the complete value, including prefixes such as fact: or case:.')),
        'summary': (str, Field(None)),
        'search_text': (str, Field(None)),
        'topics': (list[str], Field(None, description='Optional replacement topic tags for retrieval narrowing.')),
        'star': (MemoryCapabilitiesMemoryIntrospectionProviderUpdateInputStar, Field(None, description='Optional full STAR replacement for a case memory. If provided, all four fields are required. Do not use star for fact memories.')),
    },
)

MemoryCapabilitiesMemoryIntrospectionProviderDeleteInput = _strict_model(
    'MemoryCapabilitiesMemoryIntrospectionProviderDeleteInput',
    {
        'mem_ref': (str, Field(..., description='Opaque memory ref returned by recall_memory. Copy the complete value, including prefixes such as fact: or case:.')),
        'reason': (str, Field(None)),
    },
)

MemoryCapabilitiesMemoryIntrospectionProviderSetActiveProviderInput = _strict_model(
    'MemoryCapabilitiesMemoryIntrospectionProviderSetActiveProviderInput',
    {
        'active_provider_id': (str, Field(...)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderReadInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderReadInput',
    {
        'kind': (Literal['all', 'profiles', 'families'], Field('all')),
        'query': (str, Field('')),
        'include_definitions': (bool, Field(False)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderSetProfileOverrideInputChanges = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderSetProfileOverrideInputChanges',
    {
        'display_name': (str | None, Field(None)),
        'identity_fragment': (str | None, Field(None)),
        'behavior_fragment': (str | None, Field(None)),
        'output_contract_fragment': (str | None, Field(None)),
        'preferred_endpoint_id': (str | None, Field(None)),
        'capability_groups': (list[str] | None, Field(None)),
        'default_allowed_capabilities': (list[str] | None, Field(None)),
        'skill_refs': (list[str] | None, Field(None)),
        'default_approval_policy': (dict[str, Any] | None, Field(None)),
        'workspace_policy': (dict[str, Any] | None, Field(None)),
        'workspace_environment_policy': (dict[str, Any] | None, Field(None)),
        'completion_policy': (dict[str, Any] | None, Field(None)),
        'capability_policy': (dict[str, Any] | None, Field(None)),
        'capability_guidance_overrides': (dict[str, dict[str, str]] | None, Field(None)),
        'output_policy': (dict[str, Any] | None, Field(None)),
        'metadata': (dict[str, Any] | None, Field(None)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderSetProfileOverrideInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderSetProfileOverrideInput',
    {
        'profile': (str, Field(..., description='Semantic profile name, for example software_engineering.v2_coder.')),
        'changes': (MinionV2CapabilitiesMinionV2PublicProviderSetProfileOverrideInputChanges, Field(..., description='Typed merge patch for the profile definition; null removes an optional field.')),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderResetProfileOverrideInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderResetProfileOverrideInput',
    {
        'profile': (str, Field(...)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderSetFamilyOverrideInputChanges = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderSetFamilyOverrideInputChanges',
    {
        'display_name': (str | None, Field(None)),
        'domain': (str | None, Field(None)),
        'domain_keywords': (list[str] | None, Field(None)),
        'workflow_template': (str | None, Field(None)),
        'role_bindings': (dict[str, str | None] | None, Field(None)),
        'builders': (dict[str, str | None] | None, Field(None)),
        'adapters': (dict[str, str | None] | None, Field(None)),
        'policies': (dict[str, Any] | None, Field(None)),
        'capability_groups': (dict[str, Any] | None, Field(None)),
        'metadata': (dict[str, Any] | None, Field(None)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderSetFamilyOverrideInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderSetFamilyOverrideInput',
    {
        'family': (str, Field(..., description='Semantic family name, for example software_engineering.')),
        'changes': (MinionV2CapabilitiesMinionV2PublicProviderSetFamilyOverrideInputChanges, Field(..., description='Typed merge patch for the family definition; null removes an optional field.')),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderResetFamilyOverrideInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderResetFamilyOverrideInput',
    {
        'family': (str, Field(...)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderRefreshInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderRefreshInput',
    {
    },
)

MinionV2CapabilitiesMinionV2PublicProviderSubmitArtifactInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderSubmitArtifactInput',
    {
        'name': (str, Field(...)),
        'artifact_type': (str, Field(...)),
        'schema_version': (str, Field('1')),
        'media_type': (str, Field('application/json')),
        'content': (Any, Field(...)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderSearchInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderSearchInput',
    {
        'query': (str, Field('')),
        'family_id': (str, Field(None)),
        'include_archived': (bool, Field(False)),
        'limit': (int, Field(10, ge=1, le=50)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderStatusInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderStatusInput',
    {
        'task': (str, Field(None)),
        'view': (Literal['status', 'human_review'], Field('status', description='status returns the compact projection; human_review returns the durable pending review without internal ids or tokens.')),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderResumeWorkflowInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderResumeWorkflowInput',
    {
        'task': (str, Field(None)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderRestartExecutionInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderRestartExecutionInput',
    {
        'task': (str, Field(None)),
        'reason': (str, Field(..., description='Auditable reason the current execution must be discarded and restarted.', min_length=1)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderResolveTriageInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderResolveTriageInput',
    {
        'task': (str, Field(None)),
        'subject': (str, Field(None, description='Module or phase name. Optional only when the workflow has exactly one TRIAGE_REQUIRED item.')),
        'resolution': (str, Field(..., description='Auditable summary of the external or manual action that removed the blocker.', min_length=1)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderAnswerQuestionInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderAnswerQuestionInput',
    {
        'task': (str, Field(None)),
        'answer': (str, Field(..., min_length=1)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderControlWorkflowInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderControlWorkflowInput',
    {
        'task': (str, Field(None)),
        'command': (Literal['pause', 'cancel'], Field(...)),
        'reason': (str, Field(None)),
    },
)

MinionV2CapabilitiesMinionV2PublicProviderArchiveWorkflowInput = _strict_model(
    'MinionV2CapabilitiesMinionV2PublicProviderArchiveWorkflowInput',
    {
        'task': (str, Field(None)),
        'reason': (str, Field(None)),
    },
)

PluginsCapabilitiesPluginsIntrospectionProviderAttachInput = _strict_model(
    'PluginsCapabilitiesPluginsIntrospectionProviderAttachInput',
    {
        'plugin_id': (str, Field(...)),
    },
)

PluginsCapabilitiesPluginsIntrospectionProviderDetachInput = _strict_model(
    'PluginsCapabilitiesPluginsIntrospectionProviderDetachInput',
    {
        'plugin_id': (str, Field(...)),
    },
)

PluginsCapabilitiesPluginsIntrospectionProviderEnableInput = _strict_model(
    'PluginsCapabilitiesPluginsIntrospectionProviderEnableInput',
    {
        'plugin_id': (str, Field(...)),
    },
)

PluginsCapabilitiesPluginsIntrospectionProviderDisableInput = _strict_model(
    'PluginsCapabilitiesPluginsIntrospectionProviderDisableInput',
    {
        'plugin_id': (str, Field(...)),
    },
)

PluginsL3SqliteVecSQLiteVecL3PluginRecallInput = _strict_model(
    'PluginsL3SqliteVecSQLiteVecL3PluginRecallInput',
    {
        'queries': (list[str], Field(None, description='One to three focused natural-language search strings for durable facts, preferences, project context, prior decisions, repair cases, or task experience. Include concrete names, modules, error text, symptoms, failed fixes, or user terms when known; do not paste large raw context.')),
        'topic_scope': (list[str], Field(None, description='Optional short topic keywords that narrow retrieval, such as a project, subsystem, preference area, or failure domain. This is semantic narrowing, not the storage scope.')),
        'task_id': (str, Field(None, description='Optional exact task, work order, run, or minion task identifier from current context. When provided, recall is narrowed to task-scoped memories for that task. Do not invent or guess task ids.')),
        'limit': (int, Field(None, description='Maximum memories to return. Use 3-5 by default; use a larger value only when comparing several possible matches.', ge=1, le=10)),
        'kind': (Literal['fact', 'case'], Field(None, description='Optional memory type filter. Use fact for stable facts, preferences, project context, or prior decisions. Use case for prior failures, debugging attempts, repair lessons, or task experience.')),
        'view': (Literal['summary', 'origin'], Field(None, description='Use summary by default for normal work. Use origin only when provenance, source text, or extra detail is needed to resolve a conflict, update/delete a memory safely, or audit where the memory came from.')),
    },
)

PluginsL3SqliteVecSQLiteVecL3PluginWriteInputStar = _strict_model(
    'PluginsL3SqliteVecSQLiteVecL3PluginWriteInputStar',
    {
        'situation': (str, Field(..., description='Situation or failure context.')),
        'task': (str, Field(..., description='Task or objective in that situation.')),
        'action': (str, Field(..., description='Action, repair, or decision that mattered.')),
        'result': (str, Field(..., description='Outcome or reusable lesson.')),
    },
)

PluginsL3SqliteVecSQLiteVecL3PluginWriteInput = _strict_model(
    'PluginsL3SqliteVecSQLiteVecL3PluginWriteInput',
    {
        'kind': (Literal['fact', 'case'], Field(..., description='fact or case; case requires star.')),
        'title': (str, Field(None, description='Optional short label for this memory.')),
        'summary': (str, Field(..., description='Prompt-ready memory text future Pal can read directly.')),
        'search_text': (str, Field(..., description='Retrieval/source text with concrete details for FTS and vector embedding.')),
        'task_id': (str, Field(None, description='Optional exact task/work order/run id; providing it binds the memory to task scope.')),
        'topics': (list[str], Field(None, description='Topic tags for filtering')),
        'star': (PluginsL3SqliteVecSQLiteVecL3PluginWriteInputStar, Field(None, description="Required when kind='case'; omit for fact memories.")),
    },
)

PluginsL3SqliteVecSQLiteVecL3PluginUpdateInputStar = _strict_model(
    'PluginsL3SqliteVecSQLiteVecL3PluginUpdateInputStar',
    {
        'situation': (str, Field(..., description='Situation or failure context.')),
        'task': (str, Field(..., description='Task or objective in that situation.')),
        'action': (str, Field(..., description='Action, repair, or decision that mattered.')),
        'result': (str, Field(..., description='Outcome or reusable lesson.')),
    },
)

PluginsL3SqliteVecSQLiteVecL3PluginUpdateInput = _strict_model(
    'PluginsL3SqliteVecSQLiteVecL3PluginUpdateInput',
    {
        'mem_ref': (str, Field(..., description='Opaque memory ref returned by recall_memory, such as fact:fact_abc or case:case_abc. Copy the complete value including the fact: or case: prefix.')),
        'title': (str, Field(None, description='Updated short label')),
        'summary': (str, Field(None, description='Updated concise summary')),
        'search_text': (str, Field(None, description='Updated retrieval/source text for indexing.')),
        'topics': (list[str], Field(None, description='Replacement topic tags')),
        'star': (PluginsL3SqliteVecSQLiteVecL3PluginUpdateInputStar, Field(None, description='Optional full STAR replacement for a case memory. If provided, all four fields are required.')),
    },
)

PluginsL3SqliteVecSQLiteVecL3PluginDeleteInput = _strict_model(
    'PluginsL3SqliteVecSQLiteVecL3PluginDeleteInput',
    {
        'mem_ref': (str, Field(..., description='Opaque memory ref returned by recall_memory, such as fact:fact_abc or case:case_abc. Copy the complete value including the fact: or case: prefix.')),
        'reason': (str, Field(None, description='Brief reason for deletion')),
    },
)

PluginsL3SqliteVecSQLiteVecL3PluginRefreshIndexesInput = _strict_model(
    'PluginsL3SqliteVecSQLiteVecL3PluginRefreshIndexesInput',
    {
        'limit': (int, Field(None)),
        'retry_failed': (bool, Field(None)),
    },
)

PluginsL3StubsL3ProviderCapabilityMixinRecallInput = _strict_model(
    'PluginsL3StubsL3ProviderCapabilityMixinRecallInput',
    {
        'queries': (list[str], Field(None, description='One to three focused natural-language search strings for durable facts, preferences, project context, prior decisions, repair cases, or task experience. Include concrete names, modules, error text, symptoms, failed fixes, or user terms when known; do not paste large raw context.')),
        'topic_scope': (list[str], Field(None, description='Optional short topic keywords that narrow retrieval, such as a project, subsystem, preference area, or failure domain. This is semantic narrowing, not the storage scope.')),
        'task_id': (str, Field(None, description='Optional exact task, work order, run, or minion task identifier from current context. When provided, recall is narrowed to task-scoped memories for that task. Do not invent or guess task ids.')),
        'limit': (int, Field(None, description='Maximum memories to return. Use 3-5 by default; use a larger value only when comparing several possible matches.', ge=1, le=10)),
        'view': (Literal['summary', 'origin'], Field(None, description='Use summary by default for normal work. Use origin only when provenance, source text, or extra detail is needed to resolve a conflict, update/delete a memory safely, or audit where the memory came from.')),
    },
)

PluginsL3StubsL3ProviderCapabilityMixinWriteInputStar = _strict_model(
    'PluginsL3StubsL3ProviderCapabilityMixinWriteInputStar',
    {
        'situation': (str, Field(..., description='Situation or failure context.')),
        'task': (str, Field(..., description='Task or objective in that situation.')),
        'action': (str, Field(..., description='Action, repair, or decision that mattered.')),
        'result': (str, Field(..., description='Outcome or reusable lesson.')),
    },
)

PluginsL3StubsL3ProviderCapabilityMixinWriteInput = _strict_model(
    'PluginsL3StubsL3ProviderCapabilityMixinWriteInput',
    {
        'kind': (Literal['fact', 'case'], Field(..., description='fact or case; case requires star.')),
        'title': (str, Field(None, description='Optional short label for this memory.')),
        'summary': (str, Field(..., description='Prompt-ready memory text future Pal can read directly.')),
        'search_text': (str, Field(None, description='Retrieval/source text with concrete details for indexing.')),
        'task_id': (str, Field(None, description='Optional exact task/work order/run id; providing it binds the memory to task scope.')),
        'topics': (list[str], Field(None, description='Optional short semantic topic tags.')),
        'star': (PluginsL3StubsL3ProviderCapabilityMixinWriteInputStar, Field(None, description="Required when kind='case'; omit for fact memories.")),
    },
)

PluginsL3StubsL3ProviderCapabilityMixinUpdateInputStar = _strict_model(
    'PluginsL3StubsL3ProviderCapabilityMixinUpdateInputStar',
    {
        'situation': (str, Field(..., description='Situation or failure context.')),
        'task': (str, Field(..., description='Task or objective in that situation.')),
        'action': (str, Field(..., description='Action, repair, or decision that mattered.')),
        'result': (str, Field(..., description='Outcome or reusable lesson.')),
    },
)

PluginsL3StubsL3ProviderCapabilityMixinUpdateInput = _strict_model(
    'PluginsL3StubsL3ProviderCapabilityMixinUpdateInput',
    {
        'mem_ref': (str, Field(..., description='Opaque memory ref returned by recall_memory, such as fact:fact_abc or case:case_abc. Copy the complete value including the fact: or case: prefix.')),
        'title': (str, Field(None)),
        'summary': (str, Field(None)),
        'search_text': (str, Field(None)),
        'topics': (list[str], Field(None)),
        'star': (PluginsL3StubsL3ProviderCapabilityMixinUpdateInputStar, Field(None, description='Optional full STAR replacement for a case memory. If provided, all four fields are required.')),
    },
)

PluginsL3StubsL3ProviderCapabilityMixinDeleteInput = _strict_model(
    'PluginsL3StubsL3ProviderCapabilityMixinDeleteInput',
    {
        'mem_ref': (str, Field(..., description='Opaque memory ref returned by recall_memory, such as fact:fact_abc or case:case_abc. Copy the complete value including the fact: or case: prefix.')),
        'reason': (str, Field(None, description='Brief reason for deletion')),
    },
)

PluginsL3StubsL3ProviderCapabilityMixinRefreshIndexesInput = _strict_model(
    'PluginsL3StubsL3ProviderCapabilityMixinRefreshIndexesInput',
    {
        'limit': (int, Field(None)),
        'retry_failed': (bool, Field(None)),
    },
)

ProactiveCapabilitiesProactiveIntrospectionProviderListRunsInput = _strict_model(
    'ProactiveCapabilitiesProactiveIntrospectionProviderListRunsInput',
    {
        'target_id': (str, Field(...)),
        'limit': (int, Field(None)),
    },
)

ProactiveCapabilitiesProactiveIntrospectionProviderCreateInput = _strict_model(
    'ProactiveCapabilitiesProactiveIntrospectionProviderCreateInput',
    {
        'proactive_id': (str, Field(..., description='Stable identifier for this proactive task.')),
        'goal': (str, Field(...)),
        'method': (str, Field(None)),
        'skill_refs': (list[str], Field(None)),
        'out_channel_id': (str, Field(None, description='Use channel_list to find available endpoint IDs')),
        'enabled': (bool, Field(None)),
        'out_reply_target': (dict[str, Any], Field(None, description='Query channel endpoint auth_state for routing info (session_id, request_id)')),
        'schedule': (dict[str, Any], Field(None, description='Scheduling config. cadence=\'cron\': {cadence,cron,timezone} where cron is standard 5-field expression. cadence=\'once\': {cadence,run_at_utc}. cadence=\'manual\': no schedule. Example reminder: {"cadence":"once","run_at_utc":"2026-05-12T09:00:00Z"}. Example recurring push: {"cadence":"cron","cron":"0 9 * * *","timezone":"Asia/Shanghai"}')),
    },
)

ProactiveCapabilitiesProactiveIntrospectionProviderDeleteInput = _strict_model(
    'ProactiveCapabilitiesProactiveIntrospectionProviderDeleteInput',
    {
        'target_id': (str, Field(...)),
    },
)

ProactiveCapabilitiesProactiveIntrospectionProviderEnableInput = _strict_model(
    'ProactiveCapabilitiesProactiveIntrospectionProviderEnableInput',
    {
        'target_id': (str, Field(...)),
    },
)

ProactiveCapabilitiesProactiveIntrospectionProviderDisableInput = _strict_model(
    'ProactiveCapabilitiesProactiveIntrospectionProviderDisableInput',
    {
        'target_id': (str, Field(...)),
    },
)

ProactiveCapabilitiesProactiveIntrospectionProviderSetOutputChannelInput = _strict_model(
    'ProactiveCapabilitiesProactiveIntrospectionProviderSetOutputChannelInput',
    {
        'target_id': (str, Field(...)),
        'out_channel_id': (str, Field(None)),
    },
)

ProactiveCapabilitiesProactiveIntrospectionProviderSetOutputTargetInput = _strict_model(
    'ProactiveCapabilitiesProactiveIntrospectionProviderSetOutputTargetInput',
    {
        'target_id': (str, Field(...)),
        'out_reply_target': (dict[str, Any], Field(None, description='Reply routing info: session_id, request_id')),
    },
)

ProactiveCapabilitiesProactiveIntrospectionProviderUpdateScheduleInput = _strict_model(
    'ProactiveCapabilitiesProactiveIntrospectionProviderUpdateScheduleInput',
    {
        'target_id': (str, Field(...)),
        'schedule': (dict[str, Any], Field(..., description='Scheduling config. cadence=\'cron\': {cadence,cron,timezone} where cron is standard 5-field expression. cadence=\'once\': {cadence,run_at_utc}. cadence=\'manual\': no schedule. Example reminder: {"cadence":"once","run_at_utc":"2026-05-12T09:00:00Z"}. Example recurring push: {"cadence":"cron","cron":"0 9 * * *","timezone":"Asia/Shanghai"}')),
    },
)

SkillCapabilitiesSkillIntrospectionProviderAssimilateInput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderAssimilateInput',
    {
        'source_text': (str, Field(..., description='Text or SKILL.md content to turn into an optional Pal operation manual candidate.')),
        'source_format': (Literal['plain_text', 'skill_md'], Field('plain_text')),
        'intent': (Literal['learn', 'summarize', 'sanitize'], Field('learn')),
        'desired_skill_id': (str, Field(None)),
        'source_refs': (list[str], Field(None)),
        'source_metadata': (dict[str, Any], Field(None)),
    },
)

SkillCapabilitiesSkillIntrospectionProviderAssimilateOutput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderAssimilateOutput',
    {
    },
)

SkillCapabilitiesSkillIntrospectionProviderCommitInput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderCommitInput',
    {
        'candidate_id': (str, Field(None, description='Pending skill candidate id to save. Prefer this when the user provides candidate_id; no candidate object is needed.')),
        'candidate': (dict[str, Any], Field(None, description='Inline candidate object. Use only when no candidate_id is available.')),
        'replace': (bool, Field(False)),
    },
)

SkillCapabilitiesSkillIntrospectionProviderCommitOutput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderCommitOutput',
    {
    },
)

SkillCapabilitiesSkillIntrospectionProviderUpdateInput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderUpdateInput',
    {
        'skill_id': (str, Field(...)),
        'patch': (dict[str, Any], Field(...)),
    },
)

SkillCapabilitiesSkillIntrospectionProviderUpdateOutput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderUpdateOutput',
    {
    },
)

SkillCapabilitiesSkillIntrospectionProviderDisableInput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderDisableInput',
    {
        'skill_id': (str, Field(...)),
    },
)

SkillCapabilitiesSkillIntrospectionProviderDisableOutput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderDisableOutput',
    {
    },
)

SkillCapabilitiesSkillIntrospectionProviderSearchInput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderSearchInput',
    {
        'query': (str, Field(..., description='Current scenario, user request, or explicit skill name to match.')),
        'status': (str, Field('active')),
        'top_k': (int, Field(5, ge=1)),
    },
)

SkillCapabilitiesSkillIntrospectionProviderSearchOutput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderSearchOutput',
    {
    },
)

SkillCapabilitiesSkillIntrospectionProviderReadInput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderReadInput',
    {
        'skill_id': (str, Field(...)),
        'include_manual': (bool, Field(False)),
    },
)

SkillCapabilitiesSkillIntrospectionProviderReadOutput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderReadOutput',
    {
    },
)

SkillCapabilitiesSkillIntrospectionProviderInjectInput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderInjectInput',
    {
        'skill_id': (str, Field(..., description='Skill id to inject.')),
    },
)

SkillCapabilitiesSkillIntrospectionProviderInjectOutput = _strict_model(
    'SkillCapabilitiesSkillIntrospectionProviderInjectOutput',
    {
        'skill_id': (str, Field(None)),
        'title': (str, Field(None)),
        'summary': (str, Field(None)),
        'status': (str, Field(None)),
        'use_when': (str, Field(None)),
        'avoid_when': (str, Field(None)),
        'applicability_star': (dict[str, Any], Field(None)),
        'manual_text': (str, Field(None)),
        'capability_refs': (list[str], Field(None)),
    },
)

WebFetchCapabilitiesWebFetchIntrospectionProviderSetActiveProviderInput = _strict_model(
    'WebFetchCapabilitiesWebFetchIntrospectionProviderSetActiveProviderInput',
    {
        'active_provider_id': (str, Field(...)),
    },
)

WebFetchCapabilitiesWebFetchIntrospectionProviderReadInput = _strict_model(
    'WebFetchCapabilitiesWebFetchIntrospectionProviderReadInput',
    {
        'url': (str, Field(..., description='Absolute webpage URL to fetch.')),
        'timeout_ms': (int, Field(None, description='Fetch timeout in milliseconds. Leave unset for the default unless the page is known to be slow.', ge=1000)),
        'max_chars': (int, Field(None, description='Maximum rendered text characters returned to the model. Use the default unless a longer page excerpt is needed.', ge=1000)),
        'max_raw_chars': (int, Field(None, description='Maximum raw HTML characters retained in structured output. Leave unset for normal reading.', ge=0)),
        'max_links': (int, Field(None, description='Maximum extracted links to include. Use 0 when links are irrelevant.', ge=0)),
        'user_agent': (str, Field(None, description='Optional browser user agent override. Leave unset unless a site requires a specific user agent.')),
    },
)

WebFetchCapabilitiesWebFetchIntrospectionProviderScreenshotInput = _strict_model(
    'WebFetchCapabilitiesWebFetchIntrospectionProviderScreenshotInput',
    {
        'url': (str, Field(..., description='URL to render and capture.')),
        'full_page': (bool, Field(None, description='Capture the full scrollable page instead of the viewport.')),
        'viewport_width': (int, Field(None, description='Browser viewport width in pixels.', ge=320)),
        'viewport_height': (int, Field(None, description='Browser viewport height in pixels.', ge=320)),
        'timeout_ms': (int, Field(None, description='Navigation timeout in milliseconds.', ge=1000)),
        'user_agent': (str, Field(None, description='Optional browser user agent.')),
    },
)

WebFetchCapabilitiesWebFetchIntrospectionProviderScreenshotOutput = _strict_model(
    'WebFetchCapabilitiesWebFetchIntrospectionProviderScreenshotOutput',
    {
        'artifact_id': (str, Field(None)),
        'stored_artifact_id': (str, Field(None)),
        'local_cached_path': (str, Field(None)),
        'mime_type': (str, Field(None)),
        'size_bytes': (int, Field(None)),
        'sha256': (str, Field(None)),
        'requested_url': (str, Field(None)),
        'final_url': (str, Field(None)),
        'title': (str, Field(None)),
        'status_code': (int, Field(None)),
        'configured_provider_id': (str, Field(None)),
        'effective_provider_id': (str, Field(None)),
        'fallback_used': (bool, Field(None)),
        'full_page': (bool, Field(None)),
        'viewport_width': (int, Field(None)),
        'viewport_height': (int, Field(None)),
        'registered_artifact': (bool, Field(None)),
        'artifact': (dict[str, Any], Field(None)),
    },
)

WebFetchCapabilitiesWebFetchIntrospectionProviderSetAuthMaterialInput = _strict_model(
    'WebFetchCapabilitiesWebFetchIntrospectionProviderSetAuthMaterialInput',
    {
        'material': (dict[str, Any], Field(..., description='Provider-specific auth credentials (key-value pairs)')),
    },
)

WebFetchCapabilitiesWebFetchIntrospectionProviderSetConfigInput = _strict_model(
    'WebFetchCapabilitiesWebFetchIntrospectionProviderSetConfigInput',
    {
        'config': (dict[str, Any], Field(..., description='Provider-specific settings (key-value pairs)')),
    },
)

WebSearchCapabilitiesWebSearchIntrospectionProviderSetActiveProviderInput = _strict_model(
    'WebSearchCapabilitiesWebSearchIntrospectionProviderSetActiveProviderInput',
    {
        'active_provider_id': (str, Field(...)),
    },
)

WebSearchCapabilitiesWebSearchIntrospectionProviderQueryInput = _strict_model(
    'WebSearchCapabilitiesWebSearchIntrospectionProviderQueryInput',
    {
        'query': (str, Field(..., description='Focused web search query. Include concrete names, dates, versions, or source terms when known.')),
        'limit': (int, Field(None, description='Maximum search results to return. Use 3-5 for normal lookup; larger only when comparing sources.', ge=1, le=10)),
        'region': (str, Field(None, description='Optional search region/country code supported by the provider. Leave empty unless the user asks for regional results.')),
        'safe_search': (str, Field(None, description='Optional provider safe-search setting. Leave empty unless the user or task requires a specific setting.')),
    },
)

WebSearchCapabilitiesWebSearchIntrospectionProviderSetAuthMaterialInput = _strict_model(
    'WebSearchCapabilitiesWebSearchIntrospectionProviderSetAuthMaterialInput',
    {
        'material': (dict[str, Any], Field(..., description='Provider-specific auth credentials (key-value pairs)')),
    },
)

WebSearchCapabilitiesWebSearchIntrospectionProviderSetConfigInput = _strict_model(
    'WebSearchCapabilitiesWebSearchIntrospectionProviderSetConfigInput',
    {
        'config': (dict[str, Any], Field(..., description='Provider-specific settings (key-value pairs)')),
    },
)

MinionScopedExecutionOpSearchInput = _strict_model(
    'MinionScopedExecutionOpSearchInput',
    {
        'query': (str, Field(..., description='Exact text to search for.')),
        'path': (str, Field(None, description='Optional sandbox-visible file or directory path. Defaults to the project workspace.')),
        'limit': (int, Field(50, description='Maximum matches to return.', ge=1, le=500)),
    },
)

MinionScopedExecutionOpMinionArtifactWriteInput = _strict_model(
    'MinionScopedExecutionOpMinionArtifactWriteInput',
    {
        'relative_path': (str, Field(...)),
        'content': (Any, Field(...)),
        'artifact_type': (str, Field(None)),
        'role': (str, Field(None)),
    },
)

MinionV2CandidateBuilderOpMinionDeveloperTestInput = _strict_model(
    'MinionV2CandidateBuilderOpMinionDeveloperTestInput',
    {
        'name': (str, Field(..., min_length=1)),
        'command': (str, Field(..., min_length=1)),
        'description': (str, Field(None)),
        'expected_exit_codes': (list[int], Field(None)),
        'timeout_seconds': (int, Field(None, ge=1)),
    },
)

MinionV2CandidateBuilderOpMinionDeveloperCompileCheckInput = _strict_model(
    'MinionV2CandidateBuilderOpMinionDeveloperCompileCheckInput',
    {
        'name': (str, Field(..., min_length=1)),
        'command': (str, Field(..., min_length=1)),
        'description': (str, Field(None)),
        'expected_exit_codes': (list[int], Field(None)),
        'timeout_seconds': (int, Field(None, ge=1)),
    },
)

MinionV2CandidateBuilderOpMinionDeveloperLspCheckInput = _strict_model(
    'MinionV2CandidateBuilderOpMinionDeveloperLspCheckInput',
    {
        'name': (str, Field(..., min_length=1)),
        'file': (str, Field(..., min_length=1)),
        'description': (str, Field(None)),
    },
)

MinionV2CandidateBuilderOpMinionDeveloperCheckUnavailableInput = _strict_model(
    'MinionV2CandidateBuilderOpMinionDeveloperCheckUnavailableInput',
    {
        'name': (str, Field(..., min_length=1)),
        'obligation': (Literal['focused_tests', 'compile', 'warning_clean', 'lsp'], Field(...)),
        'reason': (str, Field(..., min_length=1)),
    },
)

MinionV2CandidateBuilderOpMinionCandidateSubmitInput = _strict_model(
    'MinionV2CandidateBuilderOpMinionCandidateSubmitInput',
    {
    },
)

MinionV2CandidateBuilderOpMinionCandidateReportArchitectureDefectInput = _strict_model(
    'MinionV2CandidateBuilderOpMinionCandidateReportArchitectureDefectInput',
    {
        'summary': (str, Field(..., min_length=1)),
        'source_file': (str, Field(None)),
        'path': (str, Field(None)),
        'symbol': (str, Field(None)),
        'contract_section': (str, Field(None)),
    },
)

MinionV2CandidateBuilderOpMinionCandidateRequestModuleSplitInput = _strict_model(
    'MinionV2CandidateBuilderOpMinionCandidateRequestModuleSplitInput',
    {
        'summary': (str, Field(..., min_length=1)),
        'source_file': (str, Field(None)),
        'path': (str, Field(None)),
        'symbol': (str, Field(None)),
        'contract_section': (str, Field(None)),
    },
)

MinionV2ContractBuilderOpMinionContractUnitUpsertInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractUnitUpsertInput',
    {
        'name': (str, Field(..., min_length=1)),
        'behavior_kind': (Literal['stateless', 'resource_owner', 'service', 'workflow', 'adapter'], Field(...)),
        'responsibility': (str, Field(..., min_length=1)),
        'owned_area': (list[str], Field(...)),
        'reference_only_paths': (list[str], Field(None)),
        'depends_on': (list[str], Field(...)),
    },
)

MinionV2ContractBuilderOpMinionContractUnitAddInterfaceInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractUnitAddInterfaceInput',
    {
        'unit': (str, Field(..., min_length=1)),
        'direction': (Literal['provided', 'consumed'], Field(...)),
        'name': (str, Field(..., min_length=1)),
        'data_shape': (str, Field(None)),
        'valid_when': (str, Field(None)),
        'lifetime': (str, Field(None)),
        'ownership': (str, Field(None)),
        'error_behavior': (str, Field(None)),
        'compatibility': (str, Field(None)),
    },
)

MinionV2ContractBuilderOpMinionContractUnitSetOwnershipInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractUnitSetOwnershipInput',
    {
        'unit': (str, Field(..., min_length=1)),
        'statement': (str, Field(..., min_length=1)),
    },
)

MinionV2ContractBuilderOpMinionContractUnitSetLifecycleInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractUnitSetLifecycleInput',
    {
        'unit': (str, Field(..., min_length=1)),
        'description': (str, Field(..., min_length=1)),
        'states': (list[str], Field(None)),
        'initial_state': (str, Field(None)),
        'terminal_states': (list[str], Field(None)),
        'transitions': (list[str], Field(None)),
    },
)

MinionV2ContractBuilderOpMinionContractUnitSetStateInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractUnitSetStateInput',
    {
        'unit': (str, Field(..., min_length=1)),
        'description': (str, Field(..., min_length=1)),
        'states': (list[str], Field(None)),
        'initial_state': (str, Field(None)),
        'terminal_states': (list[str], Field(None)),
        'transitions': (list[str], Field(None)),
    },
)

MinionV2ContractBuilderOpMinionContractUnitAddRuleInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractUnitAddRuleInput',
    {
        'unit': (str, Field(..., min_length=1)),
        'kind': (Literal['invariant', 'error_behavior', 'compatibility', 'dependency_constraint', 'verification_obligation', 'split_condition'], Field(...)),
        'statement': (str, Field(..., min_length=1)),
        'condition': (str, Field(None)),
        'expected': (str, Field(None)),
    },
)

MinionV2ContractBuilderOpMinionContractUnitRemoveInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractUnitRemoveInput',
    {
        'name': (str, Field(..., min_length=1)),
    },
)

MinionV2ContractBuilderOpMinionContractAddConstraintInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractAddConstraintInput',
    {
        'name': (str, Field(..., min_length=1)),
        'constraint': (str, Field(..., min_length=1)),
        'rationale': (str, Field(None)),
    },
)

MinionV2ContractBuilderOpMinionContractAddDesignDecisionInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractAddDesignDecisionInput',
    {
        'name': (str, Field(..., min_length=1)),
        'decision': (str, Field(..., min_length=1)),
        'rationale': (str, Field(..., min_length=1)),
        'downstream_impact': (str, Field(None)),
    },
)

MinionV2ContractBuilderOpMinionContractAddGateCheckInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractAddGateCheckInput',
    {
        'name': (str, Field(..., min_length=1)),
        'check': (str, Field(..., min_length=1)),
        'scope': (str, Field(None)),
    },
)

MinionV2ContractBuilderOpMinionContractAddCrossUnitContractInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractAddCrossUnitContractInput',
    {
        'producer': (str, Field(..., min_length=1)),
        'consumer': (str, Field(..., min_length=1)),
        'interface': (str, Field(..., min_length=1)),
        'data_shape': (str, Field(None)),
        'valid_when': (str, Field(None)),
        'ownership_transfer': (str, Field(None)),
        'lifecycle_handoff': (str, Field(None)),
        'compatibility': (str, Field(None)),
        'error_behavior': (str, Field(None)),
    },
)

MinionV2ContractBuilderOpMinionContractSetIntegrationInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractSetIntegrationInput',
    {
        'depends_on': (list[str], Field(...)),
        'entrypoint': (str, Field(..., min_length=1)),
        'dataflow': (list[str], Field(None)),
        'completion_condition': (str, Field(..., min_length=1)),
        'failure_behavior': (str, Field(..., min_length=1)),
    },
)

MinionV2ContractBuilderOpMinionContractAddAssumptionInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractAddAssumptionInput',
    {
        'name': (str, Field(..., min_length=1)),
        'statement': (str, Field(..., min_length=1)),
        'owner': (str, Field(None)),
        'impact': (str, Field(None)),
        'verification_plan': (str, Field(None)),
    },
)

MinionV2ContractBuilderOpMinionContractAddRiskInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractAddRiskInput',
    {
        'name': (str, Field(..., min_length=1)),
        'risk': (str, Field(..., min_length=1)),
        'severity': (Literal['low', 'medium', 'high', 'critical'], Field(...)),
        'mitigation': (str, Field(None)),
    },
)

MinionV2ContractBuilderOpMinionContractSubmitInput = _strict_model(
    'MinionV2ContractBuilderOpMinionContractSubmitInput',
    {
    },
)

MinionV2ContractBuilderOpMinionArchitectureReviewSubmitInput = _strict_model(
    'MinionV2ContractBuilderOpMinionArchitectureReviewSubmitInput',
    {
    },
)

MinionV2SkeletonBuilderOpMinionAskQuestionInput = _strict_model(
    'MinionV2SkeletonBuilderOpMinionAskQuestionInput',
    {
        'title': (str, Field(..., min_length=1)),
        'question': (str, Field(..., min_length=1)),
        'option_1': (str, Field(..., min_length=1)),
        'option_2': (str, Field(..., min_length=1)),
        'option_3': (str, Field(..., min_length=1)),
        'timeout_seconds': (float | None, Field(None, ge=1)),
    },
)

MinionV2SkeletonBuilderOpMinionArchitectureSubmitInput = _strict_model(
    'MinionV2SkeletonBuilderOpMinionArchitectureSubmitInput',
    {
    },
)

MinionV2SkeletonBuilderOpMinionArchitectureReviewFailInput = _strict_model(
    'MinionV2SkeletonBuilderOpMinionArchitectureReviewFailInput',
    {
    },
)

MinionV2SweVerificationOpMinionVerificationRequestModuleRepairInput = _strict_model(
    'MinionV2SweVerificationOpMinionVerificationRequestModuleRepairInput',
    {
    },
)

MinionV2SweVerificationOpMinionVerificationRequestDependencyRepairsInput = _strict_model(
    'MinionV2SweVerificationOpMinionVerificationRequestDependencyRepairsInput',
    {
        'modules': (list[str], Field(..., description='Semantic module names from the bound contract dependency or scenario closure.', min_length=1)),
    },
)

MinionV2SweVerificationOpMinionVerificationRequestContractRevisionInput = _strict_model(
    'MinionV2SweVerificationOpMinionVerificationRequestContractRevisionInput',
    {
    },
)

MinionV2SweVerificationOpMinionVerificationRequestArchitectureRevisionInput = _strict_model(
    'MinionV2SweVerificationOpMinionVerificationRequestArchitectureRevisionInput',
    {
    },
)

MinionV2SweVerificationOpMinionVerificationRequestRequirementsRevisionInput = _strict_model(
    'MinionV2SweVerificationOpMinionVerificationRequestRequirementsRevisionInput',
    {
    },
)

MinionV2SweVerificationOpMinionVerificationUnknownInput = _strict_model(
    'MinionV2SweVerificationOpMinionVerificationUnknownInput',
    {
        'reason': (str, Field(..., description='Unavailable environment or platform evidence and the concrete follow-up verification plan.', min_length=1)),
    },
)

MinionV2VerificationBuilderOpMinionVerificationScratchWriteInput = _strict_model(
    'MinionV2VerificationBuilderOpMinionVerificationScratchWriteInput',
    {
        'path': (str, Field(..., min_length=1)),
        'content': (str, Field(...)),
    },
)

MinionV2VerificationBuilderOpMinionVerificationRunLspCheckInput = _strict_model(
    'MinionV2VerificationBuilderOpMinionVerificationRunLspCheckInput',
    {
        'name': (str, Field(..., min_length=1)),
        'file': (str, Field(..., min_length=1)),
        'description': (str, Field(None)),
    },
)

MinionV2VerificationBuilderOpMinionVerificationCheckUnavailableInput = _strict_model(
    'MinionV2VerificationBuilderOpMinionVerificationCheckUnavailableInput',
    {
        'name': (str, Field(..., min_length=1)),
        'obligation': (Literal['focused_tests', 'warning_clean', 'consumer_probe', 'public_surface_dogfood', 'lsp', 'historical_regressions', 'platform_probe'], Field(...)),
        'reason': (str, Field(..., min_length=1)),
        'path': (str, Field(None)),
    },
)

MinionV2VerificationBuilderOpMinionVerificationSetSummaryInput = _strict_model(
    'MinionV2VerificationBuilderOpMinionVerificationSetSummaryInput',
    {
        'summary': (str, Field(..., min_length=1)),
    },
)

MinionV2VerificationBuilderOpMinionVerificationDraftStatusInput = _strict_model(
    'MinionV2VerificationBuilderOpMinionVerificationDraftStatusInput',
    {
    },
)

MinionV2VerificationBuilderOpMinionVerificationRemoveCaseInput = _strict_model(
    'MinionV2VerificationBuilderOpMinionVerificationRemoveCaseInput',
    {
        'name': (str, Field(..., min_length=1)),
        'reason': (str, Field(..., min_length=1)),
    },
)

MinionV2VerificationBuilderOpMinionVerificationRemoveFindingInput = _strict_model(
    'MinionV2VerificationBuilderOpMinionVerificationRemoveFindingInput',
    {
        'finding_key': (str, Field(..., min_length=3, max_length=96, pattern='^[a-z][a-z0-9_]*$')),
        'reason': (str, Field(..., min_length=1)),
    },
)

MinionV2VerificationBuilderOpMinionVerificationSubmitInput = _strict_model(
    'MinionV2VerificationBuilderOpMinionVerificationSubmitInput',
    {
    },
)

MinionV2VerificationBuilderOpMinionReviewSurfaceInput = _strict_model(
    'MinionV2VerificationBuilderOpMinionReviewSurfaceInput',
    {
        'kind': (Literal['reviewed', 'test_gap', 'unreviewed', 'residual_risk'], Field(...)),
        'text': (str, Field(..., min_length=1)),
    },
)

MinionV2VerificationBuilderOpMinionReviewConclusionInput = _strict_model(
    'MinionV2VerificationBuilderOpMinionReviewConclusionInput',
    {
        'verdict': (Literal['approved', 'changes_requested', 'blocked'], Field(...)),
        'summary': (str, Field(..., min_length=1)),
        'scope': (str, Field(None)),
    },
)

MinionV2VerificationBuilderOpMinionStandaloneReviewSubmitInput = _strict_model(
    'MinionV2VerificationBuilderOpMinionStandaloneReviewSubmitInput',
    {
    },
)

MinionV2VerificationBuilderVERIFICATIONBUILDERTOOLSPECSInput = _strict_model(
    'MinionV2VerificationBuilderVERIFICATIONBUILDERTOOLSPECSInput',
    {
        'name': (str, Field(..., min_length=1)),
        'command': (str, Field(..., min_length=1)),
        'description': (str, Field(None)),
        'expected_exit_codes': (list[int], Field(None)),
        'timeout_seconds': (int, Field(None, ge=1)),
        'path': (str, Field(None)),
        'symbol': (str, Field(None)),
        'contract_section': (str, Field(None)),
        'invariants': (list[str], Field(None)),
        'probe_path': (str, Field(None)),
    },
)

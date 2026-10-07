"""Contract-review regressions assert the final model-facing result."""
from __future__ import annotations

import errno
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import Field

from pal.core import PalCore
from pal.execution import register_with_core
from pal.execution.contracts import CapabilityResult
from pal.execution.generated_tool_models import ProactiveCapabilitiesProactiveIntrospectionProviderCreateInput
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import EmptyToolInput, StrictToolModel, ToolGuidance
from pal.shared import RuntimeStatus
from pal.shared.tool_protocol import ToolAffordance, new_tool_call
from tests.capability_fixture import mount_test_capability


@pytest.fixture
def runtime():
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities("execution")
    core.context.execution_runtime.begin_tool_result_turn(turn_id="review", scope_key="contract-review")
    yield core.context.execution_runtime
    core.context.execution_runtime.shutdown()


def invoke(runtime, name, args):
    if name in runtime.registry_generation.direct_aliases:
        return runtime.execute_tool(new_tool_call(name=name, args=args), turn_id="review")
    return runtime.execute_tool(new_tool_call(name="call_tool", args={"name": name, "args": args}), turn_id="review")


@pytest.mark.parametrize("number,message", [(errno.EACCES, "Permission denied"), (errno.EROFS, "Read-only file system"), (errno.ENOSPC, "No space left on device")])
def test_write_failure_retains_cause_and_commit_uncertainty(runtime, tmp_path, monkeypatch, number, message):
    def fail(*args, **kwargs):
        raise OSError(number, message)
    monkeypatch.setattr("pal.execution.file_write.atomic_compare_and_swap_utf8", fail)
    result = invoke(runtime, "write_file", {"file_path": str(tmp_path / "new.txt"), "content": "new"})
    assert not result.ok
    assert message in result.llm_text
    assert "OSError" in result.llm_text or "PermissionError" in result.llm_text
    assert "may have committed" in result.llm_text
    assert "Read the current file" in result.llm_text


def test_delete_reports_directory_digest_rule_without_format_misdirection(runtime, tmp_path):
    directory = tmp_path / "dir"
    directory.mkdir()
    result = invoke(runtime, "delete_path", {"file_path": str(directory), "recursive": True, "expected_sha256": "0" * 64})
    assert not result.ok
    assert "only for regular files" in result.llm_text
    assert "64-character" not in result.llm_text
    assert directory.exists()


def test_delete_io_failure_and_digest_recovery_reach_model(runtime, tmp_path, monkeypatch):
    file = tmp_path / "data"
    file.write_text("retained")
    mismatch = invoke(runtime, "delete_path", {"file_path": str(file), "expected_sha256": "0" * 64})
    assert "Observed SHA-256:" in mismatch.llm_text
    assert "identity and content" in mismatch.llm_text
    def fail(*args, **kwargs):
        raise PermissionError(errno.EACCES, "unlink permission denied")
    monkeypatch.setattr(Path, "unlink", fail)
    result = invoke(runtime, "delete_path", {"file_path": str(file)})
    assert "unlink permission denied" in result.llm_text
    assert "Deletion may be partial" in result.llm_text
    assert file.exists()


class _Output(StrictToolModel):
    number: int = Field(ge=1)


def test_builtin_validation_has_reason_and_readable_output_contract(runtime):
    record = runtime.registry_generation.record_for_alias("read_tool")
    from dataclasses import replace
    failed = ExecutionRuntime._complete_builtin(replace(record, output_model=_Output, output_schema=_Output.model_json_schema()), {"number": "bad"})
    visible = ExecutionRuntime._render_invocation_for_llm(failed)
    assert "number" in visible
    assert "integer" in visible
    assert failed.affordances[0].arguments["view"] == "output"
    ordinary = runtime.execute_tool(new_tool_call(name="read_tool", args={"name": "read_file"}))
    diagnostic = runtime.execute_tool(new_tool_call(name="read_tool", args={"name": "read_file", "view": "output"}))
    full = runtime.execute_tool(new_tool_call(name="read_tool", args={"name": "read_file", "view": "full"}))
    assert '"output_schema"' not in ordinary.llm_text
    assert '"output_schema"' in diagnostic.llm_text
    assert '"input_schema"' not in diagnostic.llm_text
    assert '"input_schema"' in full.llm_text and '"output_schema"' in full.llm_text


def test_filtered_handler_recovery_uses_declared_fallback_without_widening_permissions(runtime):
    mount_test_capability(runtime, alias="failed_probe", canonical_path="op_test_failed_probe",
        InputModel=EmptyToolInput, OutputModel=_Output,
        guidance=ToolGuidance(purpose="Probe", use_when="Test", do_not_use_when="Other work", failure_next_steps="Inspect the existing local report."),
        handler=lambda _: CapabilityResult(status=RuntimeStatus.ERROR, llm_text="probe unavailable",
            affordances=(ToolAffordance(tool="forbidden_action", reason="Unavailable route"),)))
    result = invoke(runtime, "failed_probe", {})
    assert "Inspect the existing local report." in result.llm_text
    assert "forbidden_action" not in result.llm_text
    assert not result.invocation_result.affordances


class _TargetName(StrictToolModel):
    name: str


def test_unknown_target_lists_valid_values_without_a_discovery_tool(runtime):
    for name in ('alpha', 'beta'):
        mount_test_capability(runtime, alias='inspect_review_target', canonical_path='intro_test_review_target',
            InputModel=_TargetName, OutputModel=_Output, target_id=name,
            metadata={'target_argument': 'name'}, examples=({'name': name},),
            handler=lambda _: {'number': 1})
    result = invoke(runtime, 'inspect_review_target', {'name': 'missing'})
    assert not result.ok
    assert 'Valid name values: alpha, beta' in result.llm_text


def test_channel_persistence_failure_keeps_cause_and_restored_state(runtime):
    from types import SimpleNamespace
    from pal.channel.capabilities import ChannelIntrospectionProvider
    from pal.shared import IntrospectionCall
    endpoint = SimpleNamespace(enabled=True)
    channel = SimpleNamespace(get_endpoint=lambda _: endpoint,
        enable_endpoint=lambda _: setattr(endpoint, 'enabled', True),
        disable_endpoint=lambda _: setattr(endpoint, 'enabled', False))
    def fail(*_):
        raise OSError(errno.ENOSPC, 'channel state disk full')
    provider = ChannelIntrospectionProvider(runtime=channel,
        repository=SimpleNamespace(get=lambda _: SimpleNamespace(channel_kind='stdio'), set_enabled=fail),
        provider_manager=SimpleNamespace())
    mount_test_capability(runtime, alias='disable_review_channel', canonical_path='op_test_disable_channel',
        InputModel=_TargetName, OutputModel=_Output, examples=({'name': 'test'},),
        handler=lambda value: provider.disable(IntrospectionCall(name='disable_review_channel', args=value.model_dump())))
    result = invoke(runtime, 'disable_review_channel', {'name': 'test'})
    assert not result.ok
    assert 'channel state disk full' in result.llm_text
    assert 'restored to True' in result.llm_text
    assert endpoint.enabled


@pytest.mark.parametrize("schedule", [{}, {"timezone": "UTC"}, {"cadence": "manual"}, {"cadence": "cron", "cron": "0 9 * * *"}, {"cadence": "once", "run_at_utc": "2099-01-01T12:00:00"}])
def test_schedule_branches_remain_accepted_by_schema_and_validator(schedule):
    arguments = {"name": "test", "goal": "test", "schedule": schedule}
    schema = ProactiveCapabilitiesProactiveIntrospectionProviderCreateInput.model_json_schema()
    assert Draft202012Validator(schema).is_valid(arguments)
    assert ProactiveCapabilitiesProactiveIntrospectionProviderCreateInput.model_validate(arguments).schedule


@pytest.mark.parametrize("schedule", [{"cron": "0 9 * * *"}, {"cadence": "manual", "cron": "0 9 * * *"}, {"cadence": "cron", "run_at_utc": "2099-01-01"}, {"cadence": "once", "cron": "0 9 * * *"}])
def test_schedule_schema_exposes_forbidden_combinations(schedule):
    arguments = {"name": "test", "goal": "test", "schedule": schedule}
    schema = ProactiveCapabilitiesProactiveIntrospectionProviderCreateInput.model_json_schema()
    assert not Draft202012Validator(schema).is_valid(arguments)


def test_search_word_order_and_new_object_coverage(runtime):
    from pal.execution.tool_facade import EmptyToolOutput
    for alias in ("read_browser", "read_browser_page"):
        mount_test_capability(runtime, alias=alias, canonical_path="op_test_" + alias,
            InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: {},
            guidance=ToolGuidance(purpose="Read rendered content", use_when="Read content", do_not_use_when="Mutations"))
    def search(query):
        return runtime.execute_tool(new_tool_call(name="search_tools", args={"query": query, "top_k": 100})).structured["hits"]
    forward, reverse = search("read browser"), search("browser read")
    assert [(h['alias'], h['score']) for h in forward] == [(h['alias'], h['score']) for h in reverse]
    focused = search("read browser page")
    assert focused[0]["alias"] == "read_browser_page"
    assert focused[0]["score"] > next(h["score"] for h in focused if h["alias"] == "read_browser")


def test_diagnostics_are_bounded_and_redact_authentication_material():
    from pal.shared.diagnostics import exception_diagnostic
    result = exception_diagnostic(OSError("token=hidden https://user:password@example.com " + "x" * 5000))
    assert "hidden" not in result and "user:password" not in result
    assert "OSError" in result and "truncated" in result and len(result) < 2100
    quoted = exception_diagnostic(OSError('credentials: {"api_key": "quoted secret", "access_token": "hidden"} ssh://user:password@host'))
    assert 'quoted secret' not in quoted and 'hidden' not in quoted and 'user:password' not in quoted


def test_dynamic_mcp_aliases_are_stable_bounded_and_do_not_collide_after_truncation():
    from pal.mcp.compiler import _public_alias
    first = _public_alias("call", "server" * 15, "read_file")
    second = _public_alias("call", "server" * 15, "write_file")
    assert len(first) == len(second) == 64
    assert first != second
    assert first == _public_alias("call", "server" * 15, "read_file")
    assert first.startswith("call_mcp_")
    assert _public_alias("render", "demo", "review") == "render_mcp_demo_review"


@pytest.mark.parametrize('module,owner,method,expected', [
    ('pal.llm.capabilities', 'LLMIntrospectionProvider', 'list_endpoints', 'empty'),
    ('pal.web_search.capabilities', 'WebSearchIntrospectionProvider', 'health', 'unhealthy'),
    ('pal.mcp.plugin', 'McpManagerPluginProvider', 'list_servers', 'rescan_mcp_servers'),
    ('pal.memory.capabilities', 'MemoryIntrospectionProvider', 'active_provider', 'provider'),
    ('pal.channel.capabilities', 'ChannelIntrospectionProvider', 'attach', 'no-op'),
    ('pal.plugins.capabilities', 'PluginsIntrospectionProvider', 'rescan', 'scan_errors'),
    ('pal.plugins.l3.sqlite_vec', 'SQLiteVecL3Plugin', 'inventory', 'Missing embeddings'),
    ('pal.execution.capabilities', 'ExecutionIntrospectionProvider', 'show', 'observe_core'),
    ('pal.core.capabilities', 'CoreIntrospectionProvider', 'observe', 'busy'),
    ('pal.failure.capabilities', 'FailureIntrospectionProvider', 'show', 'list_failure_reports'),
    ('pal.skill.capabilities', 'SkillIntrospectionProvider', 'disable', 'enabled=true'),
    ('pal.proactive.capabilities', 'ProactiveIntrospectionProvider', 'last_run', 'never executed'),
    ('pal.identity.capabilities', 'IdentityIntrospectionProvider', 'show', 'default identity'),
    ('pal.artifact.capabilities', 'ArtifactIntrospectionProvider', 'search', 'not proof of expiry'),
    ('pal.checklist.capabilities', 'ChecklistIntrospectionProvider', 'clear', 'no-op'),
    ('pal.bunshin.workflow_capabilities', 'BunshinPublicProvider', 'reset_family_override', 'no-op'),
    ('pal.web_fetch.capabilities', 'WebFetchIntrospectionProvider', 'close', 'no-op'),
    ('pal.web_fetch.capabilities', 'WebFetchIntrospectionProvider', 'network', 'installed=true'),
])
def test_success_guidance_is_visible_in_compiled_contract(module, owner, method, expected):
    import importlib
    from pal.execution.tool_facade import compile_tool_description
    from pal.execution.tool_semantics import INDIRECT_LOCAL_READ
    provider = getattr(importlib.import_module(module), owner)
    blueprint, = getattr(provider, method).__capability_action_blueprints__
    # This is the same description compiler used by the runtime registry.
    description = compile_tool_description(alias=blueprint.aliases[0], guidance=blueprint.guidance,
        execution=blueprint.execution or INDIRECT_LOCAL_READ, input_schema={}, output_schema={}, example=None)
    assert expected.lower() in description.lower()
    assert 'Failure next steps:' not in description


def test_mcp_normalization_collisions_have_unique_aliases_and_correct_routing():
    from pal.mcp import McpCompiler, McpDiscoverySnapshot
    from pal.mcp.model import McpToolSpec, McpPromptSpec
    from tests.test_mcp import FakeInvoker
    snapshots = (
        McpDiscoverySnapshot(server_id='a-b', transport='stdio', tools=tuple(
            McpToolSpec(name=name, input_schema={'type': 'object'}) for name in ('read.file', 'read_file')),
            prompts=(McpPromptSpec(name='review.file'), McpPromptSpec(name='review_file'))),
        McpDiscoverySnapshot(server_id='a_b', transport='stdio', tools=(McpToolSpec(name='read_file', input_schema={'type': 'object'}),)),
    )
    invoker = FakeInvoker()
    projection = McpCompiler().compile(module_id='mcp', snapshots=snapshots, invoker=invoker)
    aliases = [d.name for d in projection.mounted_subtree.descriptors]
    assert len(aliases) == len(set(aliases)) == 5
    assert all(len(name) <= 64 for name in aliases)
    reverse = McpCompiler().compile(module_id='mcp', snapshots=snapshots[::-1], invoker=invoker)
    assert aliases == [d.name for d in reverse.mounted_subtree.descriptors]
    from pal.execution.contracts import CapabilityCall
    for action in projection.mounted_subtree.bound_actions:
        action.callable(CapabilityCall(name=action.canonical_path, args={}))
    assert set((server, name) for server, name, _ in invoker.tool_calls) == {
        ('a-b', 'read.file'), ('a-b', 'read_file'), ('a_b', 'read_file')}
    assert set((server, name) for server, name, _ in invoker.prompt_calls) == {
        ('a-b', 'review.file'), ('a-b', 'review_file')}


def test_search_word_reordering_preserves_score_but_added_action_object_words_improve_coverage(runtime):
    from pal.execution.tool_facade import EmptyToolOutput
    for alias in ('browser_read', 'read_browser', 'read_browser_page'):
        mount_test_capability(runtime, alias=alias, canonical_path='op_test_' + alias,
            InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: {},
            guidance=ToolGuidance(purpose='Read page content', use_when='Read content', do_not_use_when='Other work'))
    words = invoke(runtime, 'search_tools', {'query': 'browser read', 'top_k': 10})
    scores = {hit['alias']: hit['score'] for hit in words.structured['hits']}
    assert scores['browser_read'] == scores['read_browser']
    added = invoke(runtime, 'search_tools', {'query': 'read browser page', 'top_k': 10})
    scores = {hit['alias']: hit['score'] for hit in added.structured['hits']}
    assert scores['read_browser_page'] > scores['browser_read']
    assert added.structured['hits'][0]['alias'] == 'read_browser_page'
    # Exact/prefix routing can change independently of unordered word coverage.
    exact = invoke(runtime, 'search_tools', {'query': 'read_browser_page'})
    assert exact.structured['hits'][0]['alias'] == 'read_browser_page'
    prefix = invoke(runtime, 'search_tools', {'query': 'browser_re'})
    assert prefix.structured['hits'][0]['alias'] == 'browser_read'

"""Advertised defaults and conditional inputs must be executable contracts."""
import inspect

import jsonschema
import pytest
from pydantic import BaseModel, ValidationError

from pal.core import PalCore
from pal.execution import generated_tool_models
from pal.execution.runtime import _invocation_args
from pal.web_fetch import tool_models
from pal.skill.tool_models import SkillPatch


def test_all_published_defaults_match_their_schema():
    checked = 0
    def check(node, defs):
        nonlocal checked
        if isinstance(node, dict):
            if 'default' in node:
                jsonschema.Draft202012Validator({**node, '$defs': defs}).validate(node['default'])
                checked += 1
            for value in node.values():
                check(value, defs)
        elif isinstance(node, list):
            for value in node:
                check(value, defs)
    for module in (generated_tool_models, tool_models):
        for name, model in vars(module).items():
            if name.endswith('Input') and inspect.isclass(model) and issubclass(model, BaseModel):
                schema = model.model_json_schema()
                check(schema, schema.get('$defs', {}))
    assert checked > 50


def test_omit_only_and_nullable_fields_keep_distinct_invocation_semantics():
    model = generated_tool_models.ArtifactCapabilitiesArtifactIntrospectionProviderListInput
    assert _invocation_args(model.model_validate({})) == {}
    with pytest.raises(ValidationError):
        model.model_validate({'query_context': None})
    assert 'default' not in model.model_json_schema()['properties']['query_context']
    assert _invocation_args(tool_models.BrowserReadInput(url=None))['url'] is None
    patch = SkillPatch.model_validate({'avoid_when': '', 'activation_terms': [], 'enabled': False})
    assert _invocation_args(patch) == {'avoid_when': '', 'activation_terms': [], 'enabled': False}


def test_nested_list_arguments_keep_defaults_and_explicit_nulls():
    class Item(BaseModel):
        value: str | None
        enabled: bool = False
        count: int = 0
        note: str | None = None

    class Input(BaseModel):
        items: list[Item]

    value = Input.model_validate({'items': [{'value': None}, {'value': 'x', 'note': None}]})
    assert _invocation_args(value) == {'items': [
        {'value': None, 'enabled': False, 'count': 0},
        {'value': 'x', 'enabled': False, 'count': 0, 'note': None},
    ]}


@pytest.mark.parametrize('model,good,bad', [
    (tool_models.BrowserFindInput, {'text': 'login'}, {}),
    (tool_models.BrowserFindInput, {'regex': 'log.*'}, {'text': 'login', 'regex': 'log.*'}),
    (tool_models.BrowserExtensionManageInput, {'operation': 'mount', 'path': '/tmp/ext'}, {'operation': 'mount'}),
    (tool_models.BrowserExtensionManageInput, {'operation': 'reload', 'extension_id': 'ext'}, {'operation': 'reload'}),
    (tool_models.BrowserTabsInput, {'operation': 'select', 'index': 0}, {'operation': 'select'}),
    (tool_models.BrowserFindInput, {'text': 'x' * 500}, {'text': 'x' * 501}),
    (tool_models.BrowserResizeInput, {'width': 320, 'height': 4096}, {'width': 100, 'height': 900}),
    (tool_models.BrowserScrollInput, {'dy': -100000}, {'dy': -100001}),
])
def test_conditional_and_range_contracts_agree_with_validation(model, good, bad):
    model.model_validate(good)
    jsonschema.validate(good, model.model_json_schema())
    with pytest.raises(ValidationError):
        model.model_validate(bad)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, model.model_json_schema())


def test_error_paths_preserve_original_cause_and_diagnostic_action(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from pal.shared import IntrospectionCall
    from pal.memory.capabilities import MemoryIntrospectionProvider
    from pal.mcp.plugin import McpManagerPluginProvider
    from pal.mcp.normalize import normalize_protocol_error, normalize_tool_result
    context = SimpleNamespace(port_registry={})
    memory = MemoryIntrospectionProvider(service=SimpleNamespace(_resolve_l3_provider=lambda: None), context=context)
    assert 'main Pal runtime' in memory.dreaming(IntrospectionCall(name='manage_memory_dreaming')).llm_text
    assert 'No memory archive' in memory.history(IntrospectionCall(name='read_memory_history')).llm_text
    context.port_registry['memory.dreaming:dreaming'] = SimpleNamespace(configure=Mock(side_effect=ValueError('bad schedule')))
    result = memory.dreaming(IntrospectionCall(name='manage_memory_dreaming', args={'operation': 'configure', 'config': {}}))
    assert 'bad schedule' in result.llm_text
    mcp = McpManagerPluginProvider(runtime_root=tmp_path, core_context=None)
    result = mcp.image_prepare(IntrospectionCall(name='prepare_mcp_image', args={'path': str(tmp_path / 'artifact-absent.png')}))
    assert 'artifact-absent.png' in result.llm_text and 'run_shell' in result.recovery_hint
    missing = mcp.image_prepare(IntrospectionCall(name='prepare_mcp_image', args={}))
    assert 'read_tool' in missing.recovery_hint
    assert 'ValueError' != result.llm_text
    error = normalize_protocol_error(ConnectionError('connection refused'), server_id='demo', name='inspect', kind='tool')
    assert 'connection refused' in error.llm_text and "read_mcp_server(name='demo')" in error.llm_text
    error = normalize_tool_result({'isError': True, 'content': [{'type': 'text', 'text': 'invalid field'}]}, server_id='demo', tool_name='inspect')
    assert 'invalid field' in error.llm_text and 'read_tool' in error.llm_text

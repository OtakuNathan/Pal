from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pal.core import PalCore
from pal.execution import register_with_core
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, ToolGuidance
from pal.lsp import build_lsp_plugin
from pal.plugins.capabilities import register_with_core as register_plugins
from pal.shared.tool_protocol import new_tool_call
from tests.capability_fixture import mount_test_capability


@pytest.fixture
def runtime(tmp_path):
    core = PalCore()
    register_with_core(core.context)
    build_lsp_plugin(runtime_root=tmp_path).register_with_core(core.context)
    register_plugins(core.context, SimpleNamespace())
    for module in ('execution', 'lsp', 'plugins'):
        core.publish_module_capabilities(module)
    runtime = core.context.execution_runtime
    yield runtime
    runtime.shutdown()


@pytest.mark.parametrize('query,expected', [
    ('read file', 'read_file'), ('read files', 'read_file'), ('files read', 'read_file'),
    ('write files', 'write_file'), ('edit files', 'edit_file'),
    ('delete directories', 'delete_path'),
    ('lsp find reference', 'find_lsp_references'),
    ('lsp find references', 'find_lsp_references'),
    ('lsp find definition', 'find_lsp_definitions'),
    ('lsp find implementation', 'find_lsp_implementations'),
    ('lsp search symbol', 'search_lsp_workspace_symbols'),
    ('lsp search symbols', 'search_lsp_workspace_symbols'),
    ('lsp read diagnostic', 'read_lsp_diagnostics'),
    ('lsp find caller', 'find_lsp_incoming_calls'),
    ('lsp find callees', 'find_lsp_outgoing_calls'),
    ('reload plugins', 'reload_plugin'), ('install providers', 'install_package'),
])
def test_real_tools_recall_declared_object_forms(runtime, query, expected):
    result = runtime.execute_tool(new_tool_call(name='search_tools', args={'query': query}))
    assert result.ok
    assert result.structured['hits'][0]['alias'] == expected
    assert 'search_objects' not in result.llm_text
    assert all('search_objects' not in hit for hit in result.structured['hits'])


@pytest.mark.parametrize('query', [
    'web search', 'git status', 'read file from web', 'lsp search referenc',
    'lsps find reference', 'lsp finds reference', 'lsp searches symbol',
    'lsp readys diagnostic', 'browser_re',
])
def test_object_vocabulary_does_not_expand_action_or_domain(runtime, query):
    result = runtime.execute_tool(new_tool_call(name='search_tools', args={'query': query, 'top_k': 100}))
    assert result.structured['hits'] == []


def test_private_vocabulary_survives_worker_specs_but_not_model_surfaces(runtime):
    from pal.bunshin.tool_guidance import bunshin_tool_guidance

    mount_test_capability(runtime, alias='read_widget', canonical_path='op_test_read_widget',
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: {},
        guidance=ToolGuidance(purpose='Read a widget', use_when='Requested',
                              do_not_use_when='Other tasks', search_objects=('widget', 'widgets')))
    search = runtime.execute_tool(new_tool_call(name='search_tools', args={'query': 'read widgets'}))
    assert search.structured['hits'][0]['alias'] == 'read_widget'
    assert 'search_objects' not in json.dumps(search.structured)
    read = runtime.execute_tool(new_tool_call(name='read_tool', args={'name': 'read_widget'}))
    inventory = runtime.list_tool_specs()
    for payload in (read.structured, inventory, str(runtime.registry_generation.provider_specs)):
        text = json.dumps(payload)
        assert 'search_objects' not in text
        assert 'widgets' not in text
    spec = runtime.get_capability_spec('read_widget')
    restored = ToolGuidance.model_validate_json(json.dumps(spec['guidance']), strict=True)
    assert restored.search_objects == ('widget', 'widgets')
    assert bunshin_tool_guidance('op_test_read_widget', restored).search_objects == restored.search_objects


@pytest.mark.parametrize('objects', [('files', 'files'), ('read file',), ('File',), ('',)])
def test_object_contract_rejects_non_words_or_duplicates(objects):
    with pytest.raises(ValidationError, match='search_objects'):
        ToolGuidance(purpose='Read a file', use_when='Requested', do_not_use_when='Other', search_objects=objects)


def test_all_owned_guidance_declarations_explicitly_review_object_vocabulary():
    root = Path(__file__).resolve().parents[1] / 'src' / 'pal'
    found = 0
    for path in root.rglob('*.py'):
        tree = ast.parse(path.read_text())
        parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == 'ToolGuidance'):
                continue
            values = {k.arg: k.value for k in node.keywords}
            assert 'search_objects' in values, f'{path}:{node.lineno}'
            objects = ast.literal_eval(values['search_objects'])
            ToolGuidance(purpose='Contract audit', use_when='Requested', do_not_use_when='Other', search_objects=objects)
            parent = parents.get(node)
            owner = parents.get(parent)
            if isinstance(owner, ast.Call):
                aliases = next((k.value for k in owner.keywords if k.arg == 'aliases'), None)
                if aliases is not None:
                    for alias in ast.literal_eval(aliases):
                        assert alias.split('_')[0] not in objects, (alias, objects)
            found += 1
    assert found > 150


def test_workflow_specs_keep_object_forms_in_the_worker_registry():
    from pal.bunshin.candidate_builder import CANDIDATE_BUILDER_TOOL_SPECS
    from pal.bunshin.scoped_execution import _workflow_capability
    from pal.shared import MountedSubtreeHandle

    name = 'op_bunshin_candidate_submit'
    descriptor, action = _workflow_capability(
        name=name, spec=CANDIDATE_BUILDER_TOOL_SPECS[name], handler=lambda *_: {})
    subtree = MountedSubtreeHandle(module_id='workflow_scoped')
    subtree.descriptors.append(descriptor)
    subtree.bound_actions.append(action)
    subtree.bound_action_keys.append((action.canonical_path, action.target_id))
    subtree.search_record_ids.append(descriptor.name)
    runtime = ExecutionRuntime()
    try:
        runtime.mount_subtree(SimpleNamespace(mounted_subtree=subtree))
        result = runtime._search_generation(runtime.registry_generation, {'query': 'submit candidates'})
        assert result['hits'][0]['alias'] == 'submit_candidate'
        assert 'search_objects' not in json.dumps(result)
    finally:
        runtime.shutdown()


def test_dictionary_workflow_contracts_explicitly_declare_objects():
    root = Path(__file__).resolve().parents[1] / 'src' / 'pal' / 'bunshin'
    found = 0
    for path in root.glob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Dict):
                continue
            fields = {key.value: value for key, value in zip(node.keys, node.values)
                      if isinstance(key, ast.Constant)}
            if 'alias' not in fields or not isinstance(fields.get('guidance'), ast.Dict):
                continue
            guidance = fields['guidance']
            values = {key.value: value for key, value in zip(guidance.keys, guidance.values)
                      if isinstance(key, ast.Constant)}
            assert 'search_objects' in values, f'{path}:{node.lineno}'
            ToolGuidance(purpose='Audit', use_when='Requested', do_not_use_when='Other',
                         search_objects=ast.literal_eval(values['search_objects']))
            found += 1
    assert found > 20


@pytest.mark.parametrize('query', ['list task', 'task list', 'list tasks'])
def test_object_variants_rank_like_alias_words_over_incidental_purpose_words(query):
    generation = SimpleNamespace(search_records={
        'list_tasks': {'alias': 'list_tasks', 'purpose': 'List configured tasks',
                       'search_objects': ('task', 'tasks')},
        'list_runs': {'alias': 'list_runs', 'purpose': 'List run history for one task',
                      'search_objects': ('run', 'runs')},
    })
    result = ExecutionRuntime._search_generation(generation, {'query': query, 'top_k': 100})
    assert result['hits'][0]['alias'] == 'list_tasks'


@pytest.mark.parametrize('path,alias,query,expected', [
    ('web_fetch/capabilities.py', 'read_browser_page', 'browser read text', ['read_browser_page']),
    ('web_fetch/capabilities.py', 'read_browser_page', 'browser read link', ['read_browser_page']),
    ('web_fetch/capabilities.py', 'read_browser_page', 'browser read links', ['read_browser_page']),
    ('web_fetch/capabilities.py', 'navigate_browser', 'browser navigate url', ['navigate_browser']),
    ('web_fetch/capabilities.py', 'navigate_browser', 'browser navigate urls', ['navigate_browser']),
    ('artifact/capabilities.py', 'read_artifact', 'artifact read text', ['read_artifact']),
    ('artifact/capabilities.py', 'read_artifact', 'artifact read representation', ['read_artifact']),
    ('artifact/capabilities.py', 'read_artifact', 'artifact read representations', ['read_artifact']),
    ('artifact/capabilities.py', 'import_artifact', 'artifact import image', ['import_artifact']),
    ('artifact/capabilities.py', 'import_artifact', 'artifact import images', ['import_artifact']),
    ('llm/capabilities.py', 'inspect_llm_usage', 'llm inspect token', ['inspect_llm_usage']),
    ('llm/capabilities.py', 'inspect_llm_usage', 'llm inspect tokens', ['inspect_llm_usage']),
    ('web_fetch/capabilities.py', 'clear_browser_cache', 'browser clear cookies', []),
    ('artifact/capabilities.py', 'read_artifact', 'read image pixels', []),
    ('execution/file_capabilities.py', 'delete_path', 'delete link target', []),
    ('artifact/capabilities.py', 'inspect_artifact_info', 'artifact inspect representations', ['inspect_artifact_info']),
])
def test_actual_objects_are_searchable_but_negative_purpose_clauses_are_not(path, alias, query, expected):
    source = Path(__file__).resolve().parents[1] / 'src' / 'pal' / path
    for node in ast.walk(ast.parse(source.read_text())):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'capability_action'):
            continue
        fields = {key.arg: key.value for key in node.keywords}
        if alias not in ast.literal_eval(fields.get('aliases', ast.Tuple(elts=[]))):
            continue
        guidance = {key.arg: ast.literal_eval(key.value) for key in fields['guidance'].keywords
                    if key.arg in {'purpose', 'search_objects'}}
        generation = SimpleNamespace(search_records={alias: {'alias': alias, **guidance}})
        hits = ExecutionRuntime._search_generation(generation, {'query': query})['hits']
        assert [hit['alias'] for hit in hits] == expected
        exact = ExecutionRuntime._search_generation(generation, {'query': alias})['hits'][0]
        assert exact['purpose'] == guidance['purpose']
        return
    pytest.fail(f'Missing actual tool {alias}')


def test_purpose_changes_neither_recall_nor_ranking():
    records = {
        'read_widget': {'alias': 'read_widget', 'search_objects': ('widget', 'widgets')},
        'read_widget_details': {'alias': 'read_widget_details', 'search_objects': ('widget', 'widgets')},
    }
    outputs = []
    for purpose in ('Read widgets', 'Do not read cookies', 'Completely unrelated description'):
        generation = SimpleNamespace(search_records={alias: {**item, 'purpose': purpose}
                                                      for alias, item in records.items()})
        result = ExecutionRuntime._search_generation(generation, {'query': 'read widgets', 'top_k': 100})
        outputs.append([(hit['alias'], hit['score']) for hit in result['hits']])
        assert ExecutionRuntime._search_generation(generation, {'query': 'read cookies'})['hits'] == []
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0][0][0] == 'read_widget'

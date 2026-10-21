"""The same authorization engine is exported to standalone remote workers."""
import copy
import pytest

from pal.execution.file_registry import FileRegistry, FileRegistryError
from pal.execution.session_state import FileDeliveryManifest


@pytest.mark.parametrize('target', [0, 5, 'surface', '5'])
def test_file_and_shell_target_inputs_preserve_exact_identity(target):
    from pal.execution import generated_tool_models as models
    cases = ((models.ExecutionFileCapabilitiesFileCapabilityMixinReadInput, {'file_path': 'a'}),
        (models.ExecutionFileCapabilitiesFileCapabilityMixinEditInput, {'file_path': 'a', 'edits': [{'old_string': 'a', 'new_string': 'b'}]}),
        (models.ExecutionFileCapabilitiesFileCapabilityMixinWriteInput, {'file_path': 'a', 'content': 'b'}),
        (models.ExecutionShellExecShellExecCapabilityMixinShellInput, {'cmd': 'true'}))
    for model, args in cases:
        parsed = model.model_validate({**args, 'target': target})
        assert parsed.target == target and type(parsed.target) is type(target)


@pytest.mark.parametrize('target', [True, -1, 1.5, '', None])
def test_target_inputs_reject_coercion(target):
    from pydantic import ValidationError
    from pal.execution.generated_tool_models import ExecutionFileCapabilitiesFileCapabilityMixinReadInput
    with pytest.raises(ValidationError):
        ExecutionFileCapabilitiesFileCapabilityMixinReadInput.model_validate({'file_path': 'a', 'target': target})


def test_pending_ack_partial_visibility_and_terminal_retirement(tmp_path):
    path = tmp_path / 'source.txt'
    path.write_text('first\nsecond\nthird\n')
    registry = FileRegistry()
    registry.sync('scope', 0, {}, ['read'])
    read = registry.execute('scope', 0, 'read', 'read', {'file_path': str(path), 'limit': 2})
    edit = {'file_path': str(path), 'edits': [{'old_string': 'first', 'new_string': 'changed'}]}
    assert registry.execute('scope', 0, 'unacked', 'edit', edit)['status'] != 'ok'
    live = {'result': {'operation_id': 'read', 'manifest': read['context_delivery']}}
    registry.sync('scope', 1, live)
    assert registry.execute('scope', 1, 'unseen', 'edit', {
        'file_path': str(path), 'edits': [{'old_string': 'third', 'new_string': 'secret'}]})['status'] != 'ok'
    assert registry.execute('scope', 1, 'whole', 'write', {'file_path': str(path), 'content': 'whole'})['status'] != 'ok'
    registry.sync('scope', 2, {})
    with pytest.raises(FileRegistryError):
        registry.sync('scope', 1, live)
    with pytest.raises(FileRegistryError):
        registry.sync('scope', 3, live)
    assert registry.execute('scope', 2, 'retired', 'edit', edit)['status'] != 'ok'
    assert not registry.scopes['scope'].staged
    assert path.read_text() == 'first\nsecond\nthird\n'


def test_ack_duplicates_conflicts_and_multiple_references(tmp_path):
    path = tmp_path / 'source.txt'
    path.write_text('first\nsecond\n')
    registry = FileRegistry()
    registry.sync('scope', 0, {}, ['read'])
    read = registry.execute('scope', 0, 'read', 'read', {'file_path': str(path)})
    item = {'operation_id': 'read', 'manifest': read['context_delivery']}
    live = {'one': item, 'two': item}
    assert registry.sync('scope', 1, live) == registry.sync('scope', 1, live)
    with pytest.raises(FileRegistryError):
        registry.sync('scope', 1, {})
    registry.sync('scope', 2, {'two': item})
    result = registry.execute('scope', 2, 'write', 'edit', {'file_path': str(path),
        'edits': [{'old_string': 'first', 'new_string': 'updated'}]})
    assert result['status'] == 'ok', result
    assert path.read_text() == 'updated\nsecond\n'


def test_partial_line_and_forged_proof_cannot_authorize(tmp_path):
    path = tmp_path / 'source.txt'
    path.write_text('a sufficiently long source line\n')
    registry = FileRegistry()
    registry.sync('scope', 0, {}, ['read'])
    read = registry.execute('scope', 0, 'read', 'read', {'file_path': str(path)})
    original = FileDeliveryManifest.from_dict(read['context_delivery'])
    span = original.spans[0]
    partial = original.slice(span.start_offset, span.end_offset - 2)
    item = {'operation_id': 'read', 'manifest': partial.to_dict()}
    registry.sync('scope', 1, {'partial': item})
    result = registry.execute('scope', 1, 'edit', 'edit', {'file_path': str(path),
        'edits': [{'old_string': 'sufficiently', 'new_string': 'very'}]})
    assert result['status'] != 'ok'
    forged = copy.deepcopy(item)
    forged['manifest']['spans'][0]['visible_end_in_line'] += 100
    with pytest.raises(FileRegistryError):
        registry.sync('scope', 2, {'forged': forged})


def test_external_changes_and_scope_closure(tmp_path):
    path = tmp_path / 'source.txt'
    path.write_bytes(b'\xef\xbb\xbfhello\r\nworld\r\n')
    registry = FileRegistry()
    registry.sync('scope', 0, {}, ['read'])
    read = registry.execute('scope', 0, 'read', 'read', {'file_path': str(path)})
    registry.sync('scope', 1, {'r': {'operation_id': 'read', 'manifest': read['context_delivery']}})
    path.write_bytes(b'\xef\xbb\xbfexternal\r\nworld\r\n')
    assert registry.execute('scope', 1, 'edit', 'edit', {'file_path': str(path),
        'edits': [{'old_string': 'world', 'new_string': 'changed'}]})['status'] != 'ok'
    registry.sync('scope', 2, {}, closed=True)
    assert registry.sync('scope', 2, {}, closed=True) == {'revision': 2}
    with pytest.raises(FileRegistryError):
        registry.execute('scope', 2, 'read2', 'read', {'file_path': str(path)})


def test_request_projection_suspends_grants_without_retiring_results(tmp_path):
    path = tmp_path / 'source.txt'
    path.write_text('first\nsecond\n')
    registry = FileRegistry()
    registry.sync('scope', 0, {}, ['read'])
    read = registry.execute('scope', 0, 'read', 'read', {'file_path': str(path)})
    live = {'result': {'operation_id': 'read', 'manifest': read['context_delivery']}}
    edit = {'file_path': str(path), 'edits': [{'old_string': 'first', 'new_string': 'changed'}]}
    registry.sync('scope', 1, live, visible=[])
    assert registry.execute('scope', 1, 'hidden-edit', 'edit', edit)['status'] != 'ok'
    assert registry.execute('scope', 1, 'hidden-write', 'write', {
        'file_path': str(path), 'content': 'unseen'})['status'] != 'ok'
    assert path.read_text() == 'first\nsecond\n'
    registry.sync('scope', 2, live, visible=['result'])
    assert registry.execute('scope', 2, 'visible-edit', 'edit', edit)['status'] == 'ok'


def test_local_file_tools_use_the_current_request_projection(tmp_path):
    from types import SimpleNamespace
    from pal.core import PalCore
    from pal.execution import register_with_core
    from pal.shared.tool_protocol import new_tool_call
    path = tmp_path / 'source.txt'
    path.write_text('first\nsecond\n')
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    runtime = core.context.execution_runtime
    try:
        read = runtime.execute_tool(new_tool_call(name='read_file', args={'file_path': str(path)}), turn_id='turn')
        assert read.ok, read.llm_text
        runtime.commit_tool_delivery(turn_id='turn', context_delivery=read.context_delivery, result_id='read')
        runtime.prepare_model_context(None, SimpleNamespace(turn_id='turn'), context_view=SimpleNamespace(turns={}))
        edit = {'file_path': str(path), 'edits': [{'old_string': 'first', 'new_string': 'changed'}]}
        assert not runtime.execute_tool(new_tool_call(name='edit_file', args=edit), turn_id='turn').ok
        runtime.prepare_model_context(None, SimpleNamespace(turn_id='turn'),
            context_view=SimpleNamespace(turns={'turn': SimpleNamespace(tool_results={'read': 'message'})}))
        assert runtime.execute_tool(new_tool_call(name='edit_file', args=edit), turn_id='turn').ok
    finally:
        core.close()

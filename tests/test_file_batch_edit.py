from unittest.mock import patch

import pytest
from hypothesis import given, strategies as st

from pal.execution.file_edit import FileEditTool
from pal.execution.file_state import FileStateCache, atomic_compare_and_swap_utf8
from pal.execution.generated_tool_models import ExecutionFileCapabilitiesFileCapabilityMixinEditInput
from pal.shared import RuntimeStatus


def tool_for(path, text):
    path.write_text(text, encoding='utf-8', newline='')
    cache = FileStateCache()
    cache.mark_read(path, text)
    return FileEditTool(cache)


def test_batch_uses_one_cas_and_original_matching(tmp_path):
    path = tmp_path/'source'
    tool = tool_for(path, 'alpha\nbeta\ngamma\n')
    edits = [{'old_string': 'alpha', 'new_string': 'beta'}, {'old_string': 'beta', 'new_string': 'new\nlines'}]
    with patch('pal.execution.file_edit.atomic_compare_and_swap_utf8', wraps=atomic_compare_and_swap_utf8) as write:
        result = tool.invoke({'file_path': str(path), 'edits': edits})
    assert result.status == RuntimeStatus.OK
    assert write.call_count == 1
    assert path.read_text() == 'beta\nnew\nlines\ngamma\n'
    assert result.structured['edit_count'] == 2
    assert result.structured['match_count'] == 2
    assert result.context_delivery['before_digest'] != result.context_delivery['digest']


@pytest.mark.parametrize('edits,code', [
    ([{'old_string': 'alpha', 'new_string': 'x'}, {'old_string': 'missing', 'new_string': 'y'}], 'NOT_FOUND_MATCH'),
    ([{'old_string': 'alpha', 'new_string': 'x'}, {'old_string': 'lph', 'new_string': 'y'}], 'OVERLAPPING_EDITS'),
    ([{'old_string': 'alpha', 'new_string': 'x'}, {'old_string': '', 'new_string': 'y'}], 'EMPTY_OLD_STRING'),
    ([{'old_string': 'alpha', 'new_string': 'x'}, {'old_string': 'beta', 'new_string': 'beta'}], 'NO_CHANGE'),
])
def test_invalid_items_do_not_block_independent_valid_edits(tmp_path, edits, code):
    path = tmp_path/'source'
    tool = tool_for(path, 'alpha beta\n')
    result = tool.invoke({'file_path': str(path), 'edits': edits})
    failures = result.structured['failed_edits']
    assert all(item['error_code'] == code for item in failures)
    if code == 'OVERLAPPING_EDITS':
        assert result.structured['applied_edit_indices'] == []
        assert [item['edit_index'] for item in failures] == [0, 1]
        assert path.read_text() == 'alpha beta\n'
    else:
        assert result.status == RuntimeStatus.OK
        assert result.structured['applied_edit_indices'] == [0]
        assert [item['edit_index'] for item in failures] == [1]
        assert path.read_text() == 'x beta\n'


def test_replace_all_and_adjacent_delete_preserve_bom_crlf(tmp_path):
    path = tmp_path/'source'
    tool = tool_for(path, '\ufeffalpha beta\r\nalpha gamma\r\n')
    result = tool.invoke({'file_path': str(path), 'edits': [
        {'old_string': 'alpha', 'new_string': 'x', 'replace_all': True},
        {'old_string': ' beta', 'new_string': ''}]})
    assert result.status == RuntimeStatus.OK
    assert path.read_bytes() == '\ufeffx\r\nx gamma\r\n'.encode()
    assert result.structured['match_count'] == 3


def test_public_schema_rejects_legacy_and_empty_batch():
    for args in ({'file_path':'x', 'old_string':'a', 'new_string':'b'}, {'file_path':'x', 'edits':[]}):
        with pytest.raises(ValueError):
            ExecutionFileCapabilitiesFileCapabilityMixinEditInput.model_validate(args)


def test_external_change_at_commit_does_not_get_overwritten(tmp_path):
    path = tmp_path/'source'
    tool = tool_for(path, 'alpha beta')
    def external_writer(*args, **kwargs):
        path.write_text('external')
        return atomic_compare_and_swap_utf8(*args, **kwargs)
    with patch('pal.execution.file_edit.atomic_compare_and_swap_utf8', side_effect=external_writer):
        result = tool.invoke({'file_path':str(path), 'edits':[
            {'old_string':'alpha', 'new_string':'x'}, {'old_string':'beta', 'new_string':'y'}]})
    assert result.structured['error_code'] == 'STALE_FILE'
    assert path.read_text() == 'external'


@given(st.permutations(('one', 'two', 'three')))
def test_replacement_order_does_not_change_result(order):
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as root:
        path = Path(root)/'source'
        tool = tool_for(path, 'one\ntwo\nthree\n')
        result = tool.invoke({'file_path':str(path), 'edits':[
            {'old_string': word, 'new_string': word.upper()} for word in order]})
        assert result.status == RuntimeStatus.OK
        assert path.read_text() == 'ONE\nTWO\nTHREE\n'


def test_overlapping_occurrences_are_not_mistaken_for_a_unique_match(tmp_path):
    path = tmp_path/'source'
    tool = tool_for(path, 'ababa')
    for replace_all, code in ((False, 'MULTIPLE_MATCHES'), (True, 'OVERLAPPING_EDITS')):
        result = tool.invoke({'file_path':str(path), 'edits':[
            {'old_string':'aba', 'new_string':'x', 'replace_all':replace_all}]})
        assert result.structured['error_code'] == code
        assert path.read_text() == 'ababa'


def test_batch_that_cancels_itself_does_not_write(tmp_path):
    path = tmp_path/'source'
    tool = tool_for(path, 'abc')
    with patch('pal.execution.file_edit.atomic_compare_and_swap_utf8') as write:
        result = tool.invoke({'file_path':str(path), 'edits':[
            {'old_string':'ab', 'new_string':'a'}, {'old_string':'c', 'new_string':'bc'}]})
    assert result.structured['error_code'] == 'NO_CHANGE'
    write.assert_not_called()


def test_mixed_line_deltas_transform_inherited_authority(tmp_path):
    path = tmp_path/'source'
    tool = tool_for(path, 'alpha\nkeep1\nkeep2\nbeta\nkeep3\n')
    result = tool.invoke({'file_path':str(path), 'edits':[
        {'old_string':'alpha\n', 'new_string':'A\nB\n'},
        {'old_string':'beta\n', 'new_string':''}]})
    assert result.status == RuntimeStatus.OK
    assert path.read_text() == 'A\nB\nkeep1\nkeep2\nkeep3\n'
    # A newline edit also proves the adjacent boundary line through its diff.
    assert result.context_delivery['inherited_ranges'] == [[4, 4]]
    proven = {line for span in result.context_delivery['spans']
              for line in range(span['start_line'], span['end_line'] + 1)}
    assert proven | {4} == {1, 2, 3, 4, 5}


def test_reports_all_missing_ranges_and_updated_locations(tmp_path):
    path = tmp_path/'source'
    original = 'alpha\nbeta\ngamma\n'
    tool = tool_for(path, original)
    tool.cache.invalidate(path)
    tool.cache.mark_read(path, original, full_view=False, covered_ranges=((1, 1),))
    with patch('pal.execution.file_edit.atomic_compare_and_swap_utf8', wraps=atomic_compare_and_swap_utf8) as write:
        result = tool.invoke({'file_path': str(path), 'edits': [
            {'old_string': 'alpha', 'new_string': 'A\nB'},
            {'old_string': 'beta', 'new_string': 'BETA'},
            {'old_string': 'gamma', 'new_string': 'GAMMA'},
            {'old_string': 'missing', 'new_string': 'replacement'},
        ]})
    assert result.status == RuntimeStatus.OK
    assert write.call_count == 1
    assert path.read_text() == 'A\nB\nbeta\ngamma\n'
    assert result.structured['edit_count'] == 1
    assert result.structured['applied_edit_indices'] == [0]
    errors = result.structured['failed_edits']
    assert [item['edit_index'] for item in errors] == [1, 2, 3]
    assert errors[0]['original_required_line_ranges'] == [[2, 2]]
    assert errors[0]['current_match_line_ranges'] == [[3, 3]]
    assert errors[1]['current_match_line_ranges'] == [[4, 4]]
    assert errors[2]['current_match_line_ranges'] == []

    # Seeing only diagnostics must not authorize the new file content.
    from pal.execution.session_state import FileDeliveryManifest
    manifest = FileDeliveryManifest.from_dict(result.context_delivery)
    patch_offset = result.llm_text.index('--- ')
    assert manifest.slice(0, patch_offset) is None
    assert manifest.slice(patch_offset, len(result.llm_text)).spans


def test_all_conflicting_items_fail_including_nested_and_replace_all(tmp_path):
    path = tmp_path/'source'
    tool = tool_for(path, 'abcdef abc XYZ')
    result = tool.invoke({'file_path': str(path), 'edits': [
        {'old_string': 'abcdef', 'new_string': 'Q'},
        {'old_string': 'bc', 'new_string': 'R', 'replace_all': True},
        {'old_string': 'ef', 'new_string': 'S'},
        {'old_string': 'XYZ', 'new_string': 'done'},
    ]})
    assert result.structured['applied_edit_indices'] == [3]
    assert [item['edit_index'] for item in result.structured['failed_edits']] == [0, 1, 2]
    assert path.read_text() == 'abcdef abc done'


def test_all_failed_items_are_reported_without_a_write(tmp_path):
    path = tmp_path/'source'
    tool = tool_for(path, 'alpha alpha')
    with patch('pal.execution.file_edit.atomic_compare_and_swap_utf8') as write:
        result = tool.invoke({'file_path': str(path), 'edits': [
            {'old_string': '', 'new_string': 'empty'},
            {'old_string': 'alpha', 'new_string': 'ambiguous'},
            {'old_string': 'missing', 'new_string': 'absent'},
        ]})
    write.assert_not_called()
    assert result.structured['applied_edit_indices'] == []
    assert [item['error_code'] for item in result.structured['failed_edits']] == [
        'EMPTY_OLD_STRING', 'MULTIPLE_MATCHES', 'NOT_FOUND_MATCH']
    assert not result.context_delivery


def test_error_after_replacement_does_not_claim_nothing_applied(tmp_path):
    path = tmp_path/'source'
    tool = tool_for(path, 'alpha')
    def fail_after_replace(*args, **kwargs):
        atomic_compare_and_swap_utf8(*args, **kwargs)
        raise OSError('directory fsync failed')
    with patch('pal.execution.file_edit.atomic_compare_and_swap_utf8', side_effect=fail_after_replace):
        result = tool.invoke({'file_path': str(path), 'edits': [{'old_string': 'alpha', 'new_string': 'A'}]})
    assert result.structured['error_code'] == 'WRITE_FAILED'
    assert 'applied_edit_indices' not in result.structured
    assert 'uncertain' in result.llm_text
    assert path.read_text() == 'A'
    assert tool.cache.get_valid_state(path) is None


def test_partial_success_survives_runtime_output_validation(tmp_path):
    from pal.core import PalCore
    from pal.execution import register_with_core
    from pal.shared.tool_protocol import new_tool_call
    from pal.execution.tool_facade import EffectOutcome
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    runtime = core.context.execution_runtime
    runtime.begin_tool_result_turn(turn_id='t', scope_key='batch', input_id='input')
    path = tmp_path/'source'
    path.write_text('alpha\nbeta\n')
    read = runtime.execute_tool(new_tool_call(name='read_file', args={'file_path': str(path)}), turn_id='t')
    runtime.commit_tool_delivery(turn_id='t', context_delivery=dict(read.context_delivery), result_id='read')
    result = runtime.invoke_direct_tool(new_tool_call(name='edit_file', args={'file_path': str(path), 'edits': [
        {'old_string': 'alpha', 'new_string': 'A'}, {'old_string': 'missing', 'new_string': 'B'}]}), turn_id='t')
    assert result.kind == 'complete'
    assert result.effect == EffectOutcome.APPLIED
    assert result.output['applied_edit_indices'] == [0]
    assert result.output['failed_edits'][0]['edit_index'] == 1
    assert 'failed_edits' in result.llm_text and 'NOT_FOUND_MATCH' in result.llm_text
    assert path.read_text() == 'A\nbeta\n'

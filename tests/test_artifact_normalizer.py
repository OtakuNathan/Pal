"""Pending batch: normalized files and descriptors, image-only prompt expansion."""
import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from pal.artifact.capabilities import register_with_core as register_artifact
from pal.core import PalCore
from pal.execution import register_with_core
from pal.llm.ir import ArtifactRefPartIR, LLMMessageIR, MessageRole
from pal.llm.serde import message_to_payload
from pal.shared.tool_protocol import new_tool_call
from tests.test_artifact_text_access import manager, register
from tests.turn_fakes import TurnIOFixture


def test_utf8_text_has_one_managed_body_and_no_automatic_content(manager):
    content = 'BODY_MUST_NOT_BE_INJECTED\r\n' * 500
    ref = register(manager, 'notes.txt', content)
    record = manager.repository.get_record(ref.artifact_id)
    descriptor = manager.describe(ref.artifact_id, 'scope')
    assert record.original_path == record.normalized_path
    assert Path(record.original_path).read_bytes() == content.encode()
    assert not (Path(record.original_path).parent.parent / 'representations' / 'chunks').exists()
    assert descriptor['extension'] == '.txt'
    assert descriptor['files']['original']['file_path'] == descriptor['text_file']['file_path']
    assert descriptor['files']['processed'] == []
    assert 'BODY_MUST_NOT_BE_INJECTED' not in json.dumps(descriptor)
    assert 'BODY_MUST_NOT_BE_INJECTED' not in ref.summary
    exposure = manager.select_prompt_exposure('scope', 'opening', '', {'supports_vision': True})
    assert not exposure.inline_parts
    assert 'BODY_MUST_NOT_BE_INJECTED' not in exposure.text
    assert descriptor['text_file']['file_path'] in exposure.text


def test_encoding_conversion_retains_original_and_one_utf8_file(manager):
    path = manager.runtime_root / 'encoded.txt'
    text = '你好，附件。'
    path.write_bytes(text.encode('utf-16'))
    ref = manager.register_ingested(path, scope_key='scope', turn_id='opening', source_channel='test')
    description = manager.describe(ref.artifact_id, 'scope')
    assert Path(description['files']['original']['file_path']).read_bytes() == text.encode('utf-16')
    assert Path(description['text_file']['file_path']).read_bytes() == text.encode('utf-8')
    assert len(description['files']['processed']) == 1
    assert not any(rep.representation_kind == 'chunk_text' for rep in manager.repository.list_representations(ref.artifact_id))


def test_spreadsheet_is_a_typed_file_not_failed_parsing(manager):
    path = manager.runtime_root / 'report.xlsx'
    path.write_bytes(b'workbook bytes')
    ref = manager.register_ingested(path, scope_key='scope', turn_id='opening', source_channel='test')
    description = manager.describe(ref.artifact_id, 'scope')
    assert description['status'] == 'ready'
    assert description['extension'] == '.xlsx'
    assert description['mime_type'] == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    assert Path(description['files']['original']['file_path']).read_bytes() == b'workbook bytes'
    assert not description['files']['processed']
    assert 'has not been parsed' in description['handling']
    assert not manager.select_prompt_exposure('scope', 'opening', '', {'supports_vision': True}).inline_parts


def test_inventory_is_bounded_and_can_enumerate_all_files_without_refreshing_ttl(manager):
    for i in range(7):
        register(manager, f'file{i}.txt', f'content{i}')
    before = manager.repository.list_hot_states(scope_key='scope')
    first = manager.discover('scope')
    assert first['count'] == 5 and first['total'] == 7 and first['has_more'] is True
    second = manager.discover('scope', offset=first['next_offset'])
    assert second['count'] == 2 and second['has_more'] is False and second['next_offset'] is None
    assert len({item['artifact_id'] for item in first['results'] + second['results']}) == 7
    assert manager.discover('scope', query='no_matching_filename')['total'] == 0
    assert manager.discover('another-conversation')['total'] == 0
    assert manager.repository.list_hot_states(scope_key='scope') == before


def test_descriptions_and_rendering_do_not_cross_conversation_scope(manager):
    ref = register(manager, 'private.txt', 'private-body')
    with pytest.raises(KeyError, match='artifact_not_found'):
        manager.describe(ref.artifact_id, 'another-conversation')
    exposure = manager.select_prompt_exposure('another-conversation', 'opening', '', {}, artifact_ids=(ref.artifact_id,))
    assert not exposure.text and not exposure.inline_parts


def test_l1_retains_reference_while_rendering_only_images_and_manifest(manager):
    Image = pytest.importorskip('PIL.Image')
    path = manager.runtime_root / 'photo.png'
    Image.new('RGB', (8, 8), color='red').save(path)
    image = manager.register_ingested(path, scope_key='scope', turn_id='opening', source_channel='test')
    text = register(manager, 'notes.txt', 'PRIVATE_BODY')
    core = PalCore()
    register_with_core(core.context)
    register_artifact(core.context, manager)
    for module in ('execution', 'artifact'):
        core.publish_module_capabilities(module)
    try:
        message = LLMMessageIR(role=MessageRole.USER,
            parts=(ArtifactRefPartIR(artifact_id=image.artifact_id), ArtifactRefPartIR(artifact_id=text.artifact_id)))
        stored = message_to_payload(message)
        projected = core.turn_executor._project_messages_for_prompt([message], turn_id='opening',
            artifact_scope_key='scope', capabilities={'supports_vision': True})
        assert message_to_payload(message) == stored
        assert 'base64' not in json.dumps(stored)
        assert 'PRIVATE_BODY' not in projected[0].text
        assert any(part.__class__.__name__ == 'ImagePartIR' for part in projected[0].parts)
        assert 'notes.txt' in projected[0].text
        manager.policy = replace(manager.policy, image=replace(manager.policy.image, max_inline_images=0))
        no_pixels = core.turn_executor._project_messages_for_prompt([message], turn_id='opening',
            artifact_scope_key='scope', capabilities={'supports_vision': True})
        assert not any(part.__class__.__name__ == 'ImagePartIR' for part in no_pixels[0].parts)
    finally:
        core.context.execution_runtime.shutdown()


def test_four_public_tools_no_old_read_grep_select_or_list(manager):
    core = PalCore()
    register_with_core(core.context)
    register_artifact(core.context, manager)
    for module in ('execution', 'artifact'):
        core.publish_module_capabilities(module)
    runtime = core.context.execution_runtime
    runtime.register_provider_ref('core:turn_io', TurnIOFixture(scope='scope'))
    try:
        generation = runtime.registry_generation
        assert {name for name, record in generation.search_records.items() if record['module_id'] == 'artifact'} == {
            'import_artifact', 'inspect_artifact_state', 'inspect_artifact_info', 'search_artifacts'}
        for alias in ('read_artifact', 'grep_artifact', 'select_artifact', 'list_artifacts'):
            assert generation.record_for_alias(alias) is None
            assert not runtime.execute_tool(new_tool_call(name='read_tool', args={'name': alias})).ok
            assert not runtime.execute_tool(new_tool_call(name='call_tool', args={'name': alias, 'args': {}})).ok
        ref = register(manager, 'notes.txt', 'CONTENT_MUST_NOT_BE_RETURNED')
        result = asyncio.run(runtime.execute_tool_async(new_tool_call(name='call_tool', args={
            'name': 'inspect_artifact_info', 'args': {'artifact_id': ref.artifact_id}}), turn_id='opening'))
        assert result.ok, result.llm_text
        assert 'CONTENT_MUST_NOT_BE_RETURNED' not in result.llm_text
        assert result.structured['files']['original']['extension'] == '.txt'
        assert 'representations' not in result.structured
        assert 'lifecycle' not in result.structured
    finally:
        runtime.shutdown()


def test_short_legacy_transcript_stays_a_file_without_inline_text(manager):
    from pal.artifact.contracts import ArtifactRepresentation
    ref = register(manager, 'voice.wav', 'audio bytes')
    transcript = manager.runtime_root / 'transcript.txt'
    transcript.write_text('LEGACY_TRANSCRIPT_MUST_NOT_BE_INJECTED')
    manager.repository.upsert_representation(ArtifactRepresentation(
        representation_id=f'{ref.artifact_id}:transcript:default', artifact_id=ref.artifact_id,
        representation_kind='transcript', path=str(transcript), mime_type='text/plain',
        size_bytes=transcript.stat().st_size, text_preview=transcript.read_text(),
        summary=transcript.read_text(), status='ready'))
    record = manager.repository.get_record(ref.artifact_id)
    manager.repository.upsert_record(replace(record, summary=transcript.read_text()))
    exposure = manager.select_prompt_exposure('scope', 'opening', '', {'supports_vision': True})
    assert not exposure.inline_parts
    assert 'LEGACY_TRANSCRIPT_MUST_NOT_BE_INJECTED' not in exposure.text
    assert str(transcript) in exposure.text


def test_descriptions_respect_readonly_leases_and_retired_paths(manager):
    from pal.artifact import ArtifactManager
    from datetime import datetime, timedelta, timezone
    ref = register(manager, 'private.txt', 'private-body')
    read_only = ArtifactManager(runtime_root=manager.runtime_root, repository=manager.repository, writable=False)
    before = manager.repository.list_hot_states(scope_key='scope')
    assert read_only.describe(ref.artifact_id, 'scope')['files']['original']
    assert manager.repository.list_hot_states(scope_key='scope') == before
    hot = before[0]
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    manager.repository.upsert_hot_state(replace(hot, expires_at=expired))
    with pytest.raises(KeyError, match='artifact_handler_retired'):
        read_only.describe(ref.artifact_id, 'scope')
    exposure = read_only.select_prompt_exposure('scope', 'opening', '', {}, artifact_ids=(ref.artifact_id,))
    assert 'retired' in exposure.text
    assert 'original_path' not in exposure.text and 'file_path' not in exposure.text
    assert 'private-body' not in exposure.text

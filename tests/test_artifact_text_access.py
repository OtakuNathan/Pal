from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from pal.artifact import (ArtifactManager, ArtifactRepository, ArtifactRecordModel,
                          ArtifactRepresentationModel, ArtifactHotStateModel)
from pal.core import PalCore
from pal.execution import register_with_core
from pal.foundation import PalV2Database
from pal.shared.tool_protocol import new_tool_call


@pytest.fixture
def manager(tmp_path):
    database = PalV2Database(tmp_path / 'artifact.sqlite3')
    database.initialize([ArtifactRecordModel, ArtifactRepresentationModel, ArtifactHotStateModel])
    yield ArtifactManager(runtime_root=tmp_path, repository=ArtifactRepository())
    database.close()


def register(manager, name, content):
    path = manager.runtime_root / name
    path.write_text(content)
    return manager.register_ingested(path, scope_key='scope', turn_id='opening', source_channel='test')


@pytest.mark.parametrize('audio', [False, True])
def test_long_text_is_readable_beyond_artifact_preview_with_file_tool(manager, audio):
    content = ''.join(f'line{index:05d}\n' for index in range(10000))
    if audio:
        manager.transcriber = SimpleNamespace(transcribe=lambda *args, **kwargs: content)
    ref = register(manager, 'voice.wav' if audio else 'source.txt', 'fake audio' if audio else content)
    path = ref.text_file['file_path']
    assert Path(path).read_text() == content
    preview = manager.read(ref.artifact_id, 'scope', max_chars=100000)
    assert preview.truncated and len(preview.text) == 50000
    assert preview.text_file['file_path'] == path
    assert not any('artifact_transcribe' in action for action in preview.next_actions)
    exposure = manager.select_prompt_exposure('scope', 'opening', 'read attachment', {})
    assert path in exposure.text and 'read_file' in exposure.text
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    runtime = core.context.execution_runtime
    try:
        result = asyncio.run(runtime.execute_tool_async(
            new_tool_call(name='read_file', args={'file_path': path, 'offset': 9001, 'limit': 1}),
            turn_id='file-read'))
        assert result.ok, result.llm_text
        assert 'line09000' in result.llm_text
    finally:
        runtime.shutdown()


def test_duplicate_representation_hits_keep_one_body_and_all_locations(manager):
    ref = register(manager, 'unique.txt', 'a' * 6000 + ' UNIQUE_NEEDLE ' + 'b' * 30000)
    hits = manager.content_search(ref.artifact_id, 'scope', query='UNIQUE_NEEDLE', top_k=1)
    assert len(hits) == 1
    assert {item['representation'] for item in hits[0].locations} == {'text', 'chunk_text'}
    assert 'UNIQUE_NEEDLE' in hits[0].text
    assert any(item['selector'].get('chunk') == 1 for item in hits[0].locations)


def test_short_text_is_a_file_handle_without_inline_body(manager):
    content = 'first\r\n\tsecond\r\n' + 'z' * 300
    ref = register(manager, 'short.txt', content)
    exposure = manager.select_prompt_exposure('scope', 'opening', 'read attachment', {})
    assert ref.text_file['file_path'] in exposure.text
    assert 'included_text:' not in exposure.text
    assert Path(ref.text_file['file_path']).read_bytes().decode() == content
    assert manager.read(ref.artifact_id, 'scope').next_actions == ()


def test_pdf_full_text_preserves_original_page_locations(manager):
    fitz = pytest.importorskip('fitz')
    path = manager.runtime_root / 'pages.pdf'
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), 'First page line\nSecond line')
    doc.new_page()  # Blank pages must not shift the original page numbers.
    doc.new_page().insert_text((72, 72), 'Third page text')
    doc.save(path)
    doc.close()
    ref = manager.register_ingested(path, scope_key='scope', turn_id='opening', source_channel='test')
    lines = Path(ref.text_file['file_path']).read_text().splitlines()
    pages = json.loads(Path(ref.text_file['page_index_file_path']).read_text())['pages']
    assert [entry['page'] for entry in pages] == [1, 2, 3]
    assert lines[pages[0]['start_line'] - 1] == 'First page line'
    assert lines[pages[0]['end_line'] - 1] == 'Second line'
    assert lines[pages[2]['start_line'] - 1] == 'Third page text'
    assert pages[1]['has_text'] is False and pages[1]['start_line'] is None
    assert Path(ref.text_file['page_file_pattern'].format(page=2)).read_text() == ''
    assert Path(ref.text_file['page_file_pattern'].format(page=3)).read_text() == 'Third page text'
    exposure = manager.select_prompt_exposure('scope', 'opening', 'read attachment', {})
    assert ref.text_file['page_index_file_path'] in exposure.text
    assert 'start_line' not in exposure.text
    selected = manager.read(ref.artifact_id, 'scope', page=3)
    assert selected.text_file['selector'] == {'page': 3}
    assert Path(selected.text_file['file_path']).read_text() == 'Third page text'


def test_pdf_index_discloses_unprocessed_pages(manager):
    fitz = pytest.importorskip('fitz')
    manager.policy = replace(manager.policy, pdf=replace(manager.policy.pdf, max_pages=1))
    path = manager.runtime_root / 'limited.pdf'
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), 'First page')
    doc.new_page().insert_text((72, 72), 'Not extracted')
    doc.save(path)
    doc.close()
    ref = manager.register_ingested(path, scope_key='scope', turn_id='opening', source_channel='test')
    assert ref.text_file['extraction_truncated'] is True
    assert ref.text_file['page_count'] == 2 and ref.text_file['extracted_pages'] == 1
    index = json.loads(Path(ref.text_file['page_index_file_path']).read_text())
    assert [entry['page'] for entry in index['pages']] == [1]
    assert not Path(ref.text_file['page_file_pattern'].format(page=2)).exists()

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
    assert ref.available_actions == ('artifact_info',)


@pytest.mark.parametrize('text', ['', 'Short page'])
def test_pdf_keeps_page_access_and_imports_pixels_on_demand(manager, text):
    fitz = pytest.importorskip('fitz')
    path = manager.runtime_root / 'visual.pdf'
    doc = fitz.open()
    page = doc.new_page()
    if text:
        page.insert_text((72, 72), text)
    doc.new_page()
    doc.save(path)
    doc.close()
    ref = manager.register_ingested(path, scope_key='scope', turn_id='opening', source_channel='test')
    exposure = manager.select_prompt_exposure('scope', 'opening', 'read attachment', {'supports_vision': True})
    assert exposure.inline_parts == ()
    assert 'PDF pixels are not attached' in exposure.text
    assert 'artifact_import' in exposure.text
    assert 'page_index_file_path' in exposure.text
    assert 'page_file_pattern' in exposure.text
    record = manager.repository.get_record(ref.artifact_id)
    index_path = Path(record.metadata['page_index_file_path'])
    assert str(index_path) in exposure.text
    pages = json.loads(index_path.read_text())['pages']
    assert [entry['page'] for entry in pages] == [1, 2]
    assert all(Path(entry['image_file_path']).is_file() for entry in pages)
    assert pages[1]['has_text'] is False


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
    assert [entry['page'] for entry in index['pages']] == [1, 2]
    assert index['pages'][1]['status'] == 'not_processed'
    assert 'image_file_path' not in index['pages'][1]
    assert manager.repository.get_record(ref.artifact_id).status == 'partial'
    assert not Path(ref.text_file['page_file_pattern'].format(page=2)).exists()


def test_pdf_page_image_import_uses_existing_injection_path(manager):
    from pal.artifact.tools import ArtifactImportTool
    fitz = pytest.importorskip('fitz')
    path = manager.runtime_root / 'mixed.pdf'
    doc = fitz.open()
    for index in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), f'Page {index + 1} with text and graphics')
        page.draw_rect(fitz.Rect(10, 10, 50, 50), color=(1, 0, 0))
    doc.save(path)
    doc.close()
    ref = manager.register_ingested(path, scope_key='scope', turn_id='opening', source_channel='test')
    pages = json.loads(Path(ref.text_file['page_index_file_path']).read_text())['pages']
    runtime = SimpleNamespace(provider_registry={'core:turn_io': SimpleNamespace(artifact_scope_for_turn=lambda _: 'scope')})
    result = asyncio.run(ArtifactImportTool(manager).ainvoke({'path': pages[1]['image_file_path']}, runtime=runtime, turn_id='page-view'))
    assert result.status == 'ok', result.llm_text
    image_id = result.structured['artifact_id']
    assert result.context_messages[0].artifact_ids == (image_id,)
    for vision, expected in [(True, 1), (False, 0)]:
        exposure = manager.select_prompt_exposure('scope', 'page-view', 'page 2',
            {'supports_vision': vision}, artifact_ids=(image_id,))
        assert len(exposure.inline_parts) == expected
    manager.policy = replace(manager.policy, image=replace(manager.policy.image, max_inline_images=0))
    assert manager.select_prompt_exposure('scope', 'page-view', 'page 2',
        {'supports_vision': True}, artifact_ids=(image_id,)).inline_parts == ()


def test_pdf_image_failure_is_per_page_and_keeps_text(manager, monkeypatch):
    import pal.artifact.processors as processors
    fitz = pytest.importorskip('fitz')
    original = processors._render_pdf_page_images
    def render(context, record, doc, *, page_indices=None):
        if page_indices == (1,):
            raise OSError('page render failed')
        return original(context, record, doc, page_indices=page_indices)
    monkeypatch.setattr(processors, '_render_pdf_page_images', render)
    path = manager.runtime_root / 'partial.pdf'
    doc = fitz.open()
    for i in range(3):
        doc.new_page().insert_text((72, 72), f'Page {i + 1}')
    doc.save(path)
    doc.close()
    ref = manager.register_ingested(path, scope_key='scope', turn_id='opening', source_channel='test')
    pages = json.loads(Path(ref.text_file['page_index_file_path']).read_text())['pages']
    assert [page['status'] for page in pages] == ['ready', 'partial', 'ready']
    assert Path(pages[1]['file_path']).read_text() == 'Page 2'
    assert 'image_file_path' not in pages[1]
    assert manager.repository.get_record(ref.artifact_id).status == 'partial'

"""Bounded file descriptions shared by inspection and model-input rendering."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from pal.artifact.contracts import ArtifactRecord, ArtifactRepresentation


def file_description(path_text: str, mime_type: str = '') -> dict[str, Any]:
    path = Path(path_text) if path_text else None
    if path is None or not path.is_file():
        return {}
    try:
        return {'file_path': str(path.resolve()), 'file_name': path.name,
                'extension': path.suffix.lower(), 'mime_type': mime_type,
                'size_bytes': path.stat().st_size, 'read_only': True}
    except OSError:
        return {}


def artifact_summary(record: ArtifactRecord) -> str:
    # Stored legacy summaries can contain body previews. A reference or file
    # description never auto-injects document/transcript text into L1 or prompts.
    return f'{record.kind} file: {record.file_name}'


def describe_record(record: ArtifactRecord, representations: Iterable[ArtifactRepresentation]) -> dict[str, Any]:
    original = file_description(record.original_path, record.original_mime_type)
    processed = []
    text_file = {}
    for rep in representations:
        if record.kind == 'pdf':
            continue
        if rep.representation_kind not in {'text', 'transcript', 'normalized_image'}:
            continue
        file = file_description(rep.path, rep.mime_type)
        if not file:
            continue
        if rep.representation_kind in {'text', 'transcript'} and not text_file:
            text_file = {**file, 'representation': rep.representation_kind}
        if file.get('file_path') != original.get('file_path'):
            processed.append({'representation': rep.representation_kind, **file})
    result = {
        'artifact_id': record.artifact_id, 'kind': record.kind,
        'file_name': record.file_name, 'extension': Path(record.file_name).suffix.lower(),
        'mime_type': record.original_mime_type, 'size_bytes': record.original_size_bytes,
        'status': record.status, 'summary': artifact_summary(record),
        'notes': str(record.notes or '')[:2000],
        'files': {'original': original, 'processed': processed},
    }
    if text_file:
        result['text_file'] = text_file
    if record.kind == 'pdf':
        result['pdf'] = {key: record.metadata[key] for key in (
            'page_index_file_path', 'page_file_pattern', 'page_image_file_pattern',
            'page_count', 'processed_pages', 'extracted_pages', 'extraction_truncated',
        ) if key in record.metadata}
        result['handling'] = ('Read the page index and selected page text with read_file; search page files with rg. '
            'Page images are not attached automatically. Import a selected page image when pixels are needed. '
            'An empty extracted text file does not establish that its page is visually blank.')
    elif record.kind == 'audio':
        result['handling'] = ('This is an audio file, not a transcript unless text_file is present. '
            'Choose a suitable transcription tool if speech content is needed; no automatic transcription is performed.')
    elif record.kind == 'image':
        result['handling'] = 'Use attached pixels only if visual_content is attached_inline; a file path alone is not visual inspection.'
    elif record.kind == 'text' or text_file:
        result['handling'] = 'Read the supplied text_file with read_file; search it with run_shell and rg. Use bounded shell reads for oversized lines.'
    else:
        result['handling'] = 'Original file is available; content has not been parsed. Choose a tool suited to its extension and MIME type when needed.'
    return result

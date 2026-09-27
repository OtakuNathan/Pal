import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pal.bunshin.web_broker import web_result_from_payload, web_result_to_payload
from pal.core import PalCore
from pal.execution import register_with_core
from pal.shared import IntrospectionCall
from pal.shared.tool_protocol import new_tool_call
from pal.web_fetch.capabilities import WebFetchIntrospectionProvider
from pal.web_fetch import WebFetchService, register_with_core as register_web
from pal.web_fetch.browser_service import _PlaywrightCliWorker


@pytest.fixture
def runtime():
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    runtime = core.context.execution_runtime
    yield runtime
    runtime.shutdown()


@pytest.mark.parametrize('broker', [False, True])
def test_browser_full_text_is_saved_in_receiving_runtime_and_readable(runtime, broker):
    text = ''.join(f'line{i:05d}\n' for i in range(10000))
    service = SimpleNamespace(execute=lambda **kwargs: {
        'document': {'text': text[:1000], 'text_truncated': True, '_full_text': text}})
    host = WebFetchIntrospectionProvider(service=service)
    call = IntrospectionCall(name='browser_read', meta={
        'execution_runtime': runtime, 'turn_id': 'browser-turn',
        'tool_call': new_tool_call(name='browser_read', args={})})
    if broker:
        transported = host.read(IntrospectionCall(name='browser_read', meta={'broker_run_id': 'run1'}))
        assert 'text_file' not in transported.structured['document']
        assert text not in transported.llm_text
        assert runtime.result_snapshots.references() == ()
        payload = web_result_to_payload(transported)
        provider = WebFetchIntrospectionProvider(service=None, read_delegate=lambda _: web_result_from_payload(payload))
    else:
        provider = host
    result = provider.read(call)
    document = result.structured['document']
    path = Path(document['text_file']['file_path'])
    assert path.is_relative_to(runtime.result_snapshots.root)
    assert path.read_text() == text
    assert '_full_text' not in document and '_full_text' not in result.llm_text
    assert text not in result.llm_text and 'rg/read_file' in document['next_step']
    assert result.snapshot_refs[0].path == str(path)
    reread = provider.read(call)
    assert reread.structured['document']['text_file']['file_path'] != str(path)
    assert path.read_text() == text
    read = asyncio.run(runtime.execute_tool_async(new_tool_call(
        name='read_file', args={'file_path': str(path), 'offset': 9001, 'limit': 1}), turn_id='browser-turn'))
    assert read.ok, read.llm_text
    assert 'line09000' in read.llm_text


def test_browser_storage_failure_keeps_preview_without_exposing_full_body(runtime, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(runtime.result_snapshots, 'capture', fail)
    provider = WebFetchIntrospectionProvider(service=SimpleNamespace(execute=lambda **kwargs: {
        'document': {'text': 'preview', 'text_truncated': True, '_full_text': 'full private body'}}))
    result = provider.read(IntrospectionCall(name='browser_read', meta={
        'execution_runtime': runtime, 'turn_id': 'failed-storage'}))
    document = result.structured['document']
    assert document['text'] == 'preview'
    assert document['text_file_error'] == 'disk full'
    assert 'text_file' not in document and '_full_text' not in document
    assert 'full private body' not in result.llm_text
    assert 'saving the full text failed' in document['next_step']
    assert result.snapshot_refs == ()


def test_navigate_retains_snapshot_through_runtime_normalization():
    text = 'page text\n' * 1000
    manager = SimpleNamespace(execute=lambda **kwargs: {
        'document': {'text': text[:100], 'text_truncated': True, '_full_text': text}})
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    register_web(core.context, WebFetchService(browser_manager=manager))
    core.publish_module_capabilities('web_fetch')
    runtime = core.context.execution_runtime
    try:
        result = asyncio.run(runtime.execute_tool_async(new_tool_call(
            name='browser_navigate', args={'url': 'https://example.com'}), turn_id='navigation'))
        assert result.ok, result.llm_text
        assert len(result.snapshot_refs) == 1
        assert Path(result.snapshot_refs[0].path).read_text() == text
        assert '_full_text' not in result.llm_text
        assert '_full_text' not in result.structured['document']
    finally:
        runtime.shutdown()


@pytest.mark.parametrize('action', ['snapshot', 'find', 'evaluate'])
@pytest.mark.parametrize('fail_storage', [False, True])
def test_truncated_browser_operations_keep_full_output_without_reexecution(runtime, tmp_path, monkeypatch, action, fail_storage):
    worker = _PlaywrightCliWorker(runtime_root=tmp_path, max_concurrency=1)
    text = 'captured line\n' * 2000
    value = {'content': text} if action == 'evaluate' else text
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return json.dumps(value) if action == 'evaluate' else text

    monkeypatch.setattr(worker, '_run', run)
    def execute(**kwargs):
        return worker._dispatch_action(SimpleNamespace(key='a' * 64), action=kwargs['action'],
                                      args=kwargs['args'], timeout_ms=1000)

    if fail_storage:
        def fail(*args, **kwargs):
            raise OSError('disk full')
        monkeypatch.setattr(runtime.result_snapshots, 'capture', fail)
    provider = WebFetchIntrospectionProvider(service=SimpleNamespace(execute=execute))
    args = {'max_chars': 1000} if action == 'snapshot' else {'text': 'captured'} if action == 'find' else {'func': '() => document.title', 'max_chars': 200}
    result = getattr(provider, action)(IntrospectionCall(name='browser_' + action, args=args,
        meta={'execution_runtime': runtime, 'turn_id': 'captured-output'}))
    payload = result.structured
    assert len(calls) == 1
    assert payload['truncated'] is True
    assert '_full_text' not in payload and '_full_text' not in result.llm_text
    assert text not in result.llm_text
    assert 'do not repeat' in payload['next_step']
    if fail_storage:
        assert payload['text_file_error'] == 'disk full'
        assert 'text_file' not in payload
        assert result.snapshot_refs == ()
    else:
        path = Path(payload['text_file']['file_path'])
        saved = path.read_text()
        assert (json.loads(saved) if action == 'evaluate' else saved) == value
        assert result.snapshot_refs[0].path == str(path)

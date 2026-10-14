"""Browser action failures and recovery without launching a live browser."""
import json
from unittest.mock import Mock

import pytest

from pal.web_fetch.browser_service import BrowserServiceError, _PlaywrightCliWorker, _SessionRecord


def worker_and_session():
    worker = object.__new__(_PlaywrightCliWorker)
    record = _SessionRecord(key='a' * 64, name='fixture', persistent=False,
                            last_url='https://example.test/task')
    worker._ensure_session = Mock(return_value=(record, False))
    worker._launch_session_browser = Mock(return_value='')
    worker._page_state = Mock(return_value={'url': record.last_url})
    return worker, record


@pytest.mark.parametrize('cleanup_fails', [False, True])
@pytest.mark.parametrize('failed_command', ['mousedown', 'mousemove'])
def test_failed_drag_releases_pointer_and_preserves_primary_error(cleanup_fails, failed_command):
    worker, record = worker_and_session()
    commands = []
    primary = BrowserServiceError('original pointer failure', code='cli_command_failed', state_unknown=True)
    def run(_record, argv, **kwargs):
        commands.append(argv)
        if argv[0] == failed_command and len(commands) > 1:
            raise primary
        if cleanup_fails and argv[0] == 'mouseup':
            raise BrowserServiceError('release failed; token=private-value', code='cli_command_failed')
        return ''
    worker._run_write = run
    with pytest.raises(BrowserServiceError) as error:
        worker._dispatch_action(record, action='drag', args={
            'start_x': 0, 'start_y': 0, 'end_x': 100, 'end_y': 100, 'steps': 2}, timeout_ms=1000)
    assert error.value is primary and error.value.state_unknown
    assert commands[-1] == ['mouseup']
    if cleanup_fails:
        notes = '\n'.join(error.value.__notes__)
        assert 'release failed' in notes and 'private-value' not in notes


def test_drag_deadline_stops_new_moves_but_still_releases_pointer(monkeypatch):
    worker, record = worker_and_session()
    now = [0.0]
    commands = []
    monkeypatch.setattr('pal.web_fetch.browser_service.time.monotonic', lambda: now[0])
    def run(_record, argv, **kwargs):
        commands.append((argv, kwargs['timeout_ms']))
        now[0] += 0.6
        return ''
    worker._run_write = run
    with pytest.raises(BrowserServiceError) as error:
        worker._dispatch_action(record, action='drag', args={
            'start_x': 0, 'start_y': 0, 'end_x': 100, 'end_y': 100, 'steps': 40}, timeout_ms=1000)
    assert error.value.code == 'command_timeout' and error.value.state_unknown
    assert [argv[0] for argv, _ in commands] == ['mousemove', 'mousedown', 'mouseup']
    assert commands[1][1] <= 400


def test_successful_drag_releases_once_and_quotes_negative_jitter(monkeypatch):
    worker, record = worker_and_session()
    worker._run_write = Mock(return_value='')
    monkeypatch.setattr('pal.web_fetch.browser_service.random.uniform', lambda *_: -2)
    result = worker._dispatch_action(record, action='drag', args={
        'start_x': 0, 'start_y': 0, 'end_x': 0, 'end_y': 0, 'steps': 2}, timeout_ms=1000)
    commands = [call.args[1] for call in worker._run_write.call_args_list]
    assert commands[-2] == ['mousemove', '--', '0', '0']
    assert ['mousemove', '--', '-2', '-2'] in commands
    assert commands.count(['mouseup']) == 1
    assert result['drag']['end'] == [0, 0]


def test_read_fetch_recovery_restores_worker_origin_before_retry():
    worker, record = worker_and_session()
    requests = []
    commands = []
    def run(_record, argv, **kwargs):
        commands.append(argv)
        if argv[0] == 'eval' and 'fetch(' in argv[1]:
            requests.append(record.last_url)
            if len(requests) == 1:
                raise BrowserServiceError('Browser fixture is not open', code='cli_command_failed')
            return json.dumps({'status': 200, 'url': 'https://example.test/api', 'body': 'result'})
        return 'complete'
    worker._run = run
    result = worker._execute_ready(key=record.key, action='fetch', args={'url': '/api'},
                                   persistent=False, timeout_ms=1000)
    assert requests == ['https://example.test/task'] * 2
    assert commands[2] == ['goto', '--', 'https://example.test/task']
    assert result['session']['recovered'] and result['response']['body'] == 'result'


@pytest.mark.parametrize('method', ['POST', 'PATCH', 'DELETE'])
def test_write_fetch_is_not_replayed_after_browser_recovery(method):
    worker, record = worker_and_session()
    requests = []
    def run(_record, argv, **kwargs):
        if argv[0] == 'eval' and 'fetch(' in argv[1]:
            requests.append(argv)
            raise BrowserServiceError('Browser fixture is not open', code='cli_command_failed')
        return 'complete'
    worker._run = run
    with pytest.raises(BrowserServiceError) as error:
        worker._execute_ready(key=record.key, action='fetch', args={'url': '/api', 'method': method},
                              persistent=False, timeout_ms=1000)
    assert error.value.code == 'browser_recovered_action_not_replayed'
    assert error.value.state_unknown and error.value.details['browser_recovered']
    assert len(requests) == 1 and record.last_url == 'https://example.test/task'


@pytest.mark.parametrize('action', ['click', 'type', 'drag', 'evaluate'])
def test_recovery_does_not_replay_interactions_with_stale_refs_or_focus(action):
    worker, record = worker_and_session()
    worker._dispatch_action = Mock(side_effect=BrowserServiceError(
        'Browser fixture is not open', code='cli_command_failed', state_unknown=True))
    worker._run = Mock(return_value='complete')
    with pytest.raises(BrowserServiceError) as error:
        worker._execute_ready(key=record.key, action=action, args={}, persistent=False, timeout_ms=1000)
    assert error.value.code == 'browser_recovered_action_not_replayed'
    assert worker._dispatch_action.call_count == 1


def test_explicit_navigation_recovery_skips_previous_url():
    worker, record = worker_and_session()
    worker._dispatch_action = Mock(side_effect=[BrowserServiceError(
        'Browser fixture is not open', code='cli_command_failed'), {'page': {'url': 'https://new.test'}}])
    worker._run = Mock(return_value='complete')
    result = worker._execute_ready(key=record.key, action='navigate', args={'url': 'https://new.test'},
                                   persistent=False, timeout_ms=1000)
    assert result['page']['url'] == 'https://new.test'
    assert worker._launch_session_browser.call_args.kwargs['restore_page'] is False
    assert not any(call.args[1][0] == 'goto' for call in worker._run.call_args_list)


def test_failed_page_restore_does_not_retry_original_read():
    worker, record = worker_and_session()
    worker._dispatch_action = Mock(side_effect=BrowserServiceError(
        'Browser fixture is not open', code='cli_command_failed'))
    def run(_record, argv, **kwargs):
        if argv[0] == 'goto':
            raise BrowserServiceError('offline', code='command_timeout', state_unknown=True)
        return 'complete'
    worker._run = run
    with pytest.raises(BrowserServiceError) as error:
        worker._execute_ready(key=record.key, action='read', args={}, persistent=False, timeout_ms=1000)
    assert error.value.code == 'page_restore_failed'
    assert worker._dispatch_action.call_count == 1
    assert record.last_url == 'about:blank'


def test_failed_readiness_probe_is_reported_without_retrying_write(monkeypatch):
    worker, record = worker_and_session()
    worker._dispatch_action = Mock(side_effect=BrowserServiceError(
        'Browser fixture is not open', code='cli_command_failed', state_unknown=True))
    worker._run = Mock(side_effect=BrowserServiceError('not ready', code='cli_command_failed'))
    monkeypatch.setattr('pal.web_fetch.browser_service.time.sleep', lambda _: None)
    with pytest.raises(BrowserServiceError) as error:
        worker._execute_ready(key=record.key, action='click', args={}, persistent=False, timeout_ms=1000)
    assert error.value.code == 'browser_recovery_failed' and error.value.state_unknown
    assert worker._run.call_count == 5 and worker._dispatch_action.call_count == 1


def test_invalid_output_after_write_fetch_keeps_effect_unknown():
    worker, record = worker_and_session()
    worker._run = Mock(return_value='invalid response')
    with pytest.raises(BrowserServiceError) as error:
        worker._dispatch_action(record, action='fetch', args={'url': '/api', 'method': 'POST'}, timeout_ms=1000)
    assert error.value.code == 'invalid_cli_output' and error.value.state_unknown
    assert worker._run.call_count == 1

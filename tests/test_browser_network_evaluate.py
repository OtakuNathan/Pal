"""Run the production observation scripts in JS; no browser/network installation."""
import json
import shutil
import subprocess
from unittest.mock import Mock

import pytest

from pal.web_fetch.browser_service import (
    _PlaywrightCliWorker,
    _network_clear_script,
    _network_read_script,
    _network_start_script,
)


def run_network_scenario(body):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required to execute browser observation scripts')
    scripts = {
        'start': _network_start_script(),
        'clear': _network_clear_script(),
        'read': _network_read_script(url_filter='', since=0, limit=200, clear=False),
        'page': _network_read_script(url_filter='', since=0, limit=2, clear=False),
        'next': _network_read_script(url_filter='', since=2, limit=2, clear=False),
        'consume': _network_read_script(url_filter='', since=0, limit=2, clear=True),
        'filtered': _network_read_script(url_filter='/match', since=0, limit=1, clear=False),
        'filtered_next': _network_read_script(url_filter='/match', since=2, limit=1, clear=False),
        'overflow': _network_read_script(url_filter='', since=800, limit=10, clear=False),
        'expired': _network_read_script(url_filter='', since=1, limit=2, clear=False),
    }
    setup = '''
const assert = require('node:assert/strict');
global.window = {fetch: async () => ({status: 200})};
global.location = {origin: 'https://example.test'};
global.XMLHttpRequest = function() {};
XMLHttpRequest.prototype = {open() {}, send() {}, setRequestHeader() {}};
const run = name => JSON.parse(eval('(' + scripts[name] + ')')());
(async () => {
run('start');
'''
    script = 'const scripts = ' + json.dumps(scripts) + ';\n' + setup + body + '\n})();'
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('operation', ['clear', 'consume'])
def test_clear_preserves_hook_for_future_requests(operation):
    run_network_scenario('''
await window.fetch('/before');
const cleared = run(OPERATION);
await window.fetch('/after');
const page = run('read');
assert.deepEqual(page.entries.map(e => e.url), ['/after']);
assert.equal(page.entries[0].sequence, 2);
assert.equal(run('start').already_hooked, true);
'''.replace('OPERATION', json.dumps(operation)))


def test_page_cursor_does_not_skip_unreturned_records():
    run_network_scenario('''
for (let i = 0; i < 5; i++) await window.fetch('/' + i);
const first = run('page');
assert.equal(first.next_since, 2);
assert.equal(first.truncated, true);
const next = run('next');
assert.deepEqual(next.entries.map(e => e.sequence), [3, 4]);
assert.equal(next.next_since, 4);
''')


def test_clear_on_read_preserves_later_pages():
    run_network_scenario('''
for (let i = 0; i < 5; i++) await window.fetch('/' + i);
assert.equal(run('consume').next_since, 2);
assert.deepEqual(run('read').entries.map(e => e.sequence), [3, 4, 5]);
await window.fetch('/new');
assert.equal(run('read').entries.at(-1).sequence, 6);
''')


def test_filter_pagination_uses_sequence_not_matching_index():
    run_network_scenario('''
for (const url of ['/other', '/match/one', '/other', '/match/two', '/other']) await window.fetch(url);
assert.equal(run('filtered').next_since, 2);
const page = run('filtered_next');
assert.deepEqual(page.entries.map(e => e.url), ['/match/two']);
assert.equal(page.next_since, 5);
assert.equal(page.truncated, false);
''')


def test_ring_buffer_eviction_does_not_stall_cursor():
    run_network_scenario('''
for (let i = 0; i < 805; i++) await window.fetch('/' + i);
const page = run('overflow');
assert.deepEqual(page.entries.map(e => e.sequence), [801, 802, 803, 804, 805]);
assert.equal(page.next_since, 805);
assert.equal(page.cursor_expired, false);
assert.equal(run('expired').cursor_expired, true);
assert.equal(run('expired').oldest_available, 6);
''')


@pytest.mark.parametrize('value', ['x' * 5000, {'large': '中' * 5000}, list(range(1000))])
def test_evaluate_bounds_strings_objects_and_arrays(value):
    worker = object.__new__(_PlaywrightCliWorker)
    worker._run = Mock(return_value=json.dumps(value))
    result = worker._dispatch_action(Mock(), action='evaluate', args={'func': '() => value', 'max_chars': 200}, timeout_ms=1000)
    assert result['truncated'] is True
    assert len(result['result']) == 200
    assert result['result_type'] == type(value).__name__
    if not isinstance(value, str):
        assert result['result_format'] == 'json_preview'
        assert result['result'] == json.dumps(value, ensure_ascii=False)[:200]


@pytest.mark.parametrize('value', [{'ok': True}, [1, 2], 42, None, 'text'])
def test_evaluate_preserves_small_result_types(value):
    worker = object.__new__(_PlaywrightCliWorker)
    worker._run = Mock(return_value=json.dumps(value))
    result = worker._dispatch_action(Mock(), action='evaluate', args={'func': '() => value', 'max_chars': 200}, timeout_ms=1000)
    assert result['result'] == value
    assert result['truncated'] is False
    assert result['result_type'] == type(value).__name__


def test_network_body_capture_discloses_sampling_and_unavailable_streams():
    run_network_scenario('''
await window.fetch('/long', {body: 'x'.repeat(3000)});
await window.fetch({url: '/stream', body: {} });
await window.fetch('/binary', {body: new Uint8Array([1, 2])});
const entries = run('read').entries;
assert.equal(entries[0].request.body.length, 2048);
assert.equal(entries[0].request.body_original_chars, 3000);
assert.equal(entries[0].request.body_truncated, true);
assert.equal(entries[1].request.body_capture_status, 'unavailable');
assert.equal(entries[2].request.body_capture_status, 'unsupported');
''')


def test_page_and_layout_scripts_capture_all_matching_text():
    from pal.web_fetch.browser_service import _read_page_script, _layout_script
    node = shutil.which('node')
    if not node:
        pytest.skip('Node.js is required')
    scripts = {'page': _read_page_script(max_chars=1000, max_links=1),
               'none': _read_page_script(max_chars=1000, max_links=0),
               'layout': _layout_script(selector='.item', limit=1)}
    script = 'const scripts = ' + json.dumps(scripts) + ''';
const assert = require('node:assert/strict');
global.location = {href: 'https://example.test'};
const nodes = Array.from({length: 501}, (_, i) => ({
  href: 'https://example.test/' + i, innerText: 'x'.repeat(1500), rel: '',
  id: 'item' + i, tagName: 'A', getAttribute: () => '',
  getBoundingClientRect: () => ({x: i, y: 0, width: 10, height: 10})
}));
global.document = {body: {innerText: 'body'}, title: 'Title', documentElement: {lang: 'en'},
  contentType: 'text/html', querySelector: () => null, querySelectorAll: () => nodes};
global.getComputedStyle = () => ({getPropertyValue: () => ''});
const run = name => JSON.parse(eval('(' + scripts[name] + ')')());
assert.equal(run('page').links.length, 501);
assert.equal(run('page').links[500].text.length, 1500);
assert.equal(run('none').links.length, 0);
assert.equal(run('layout').elements.length, 501);
assert.equal(run('layout').elements[500].text.length, 1500);
'''
    result = subprocess.run([node, '-e', script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr

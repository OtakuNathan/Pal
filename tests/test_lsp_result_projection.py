import json

from pal.lsp.plugin import _capability_from_rpc


def test_lsp_evidence_keeps_provenance_without_repeating_result_body():
    locations = [{'uri': 'file:///repo/app.py', 'range': {'start': {'line': 8, 'character': 3}}}]
    payload = {'status': 'ok', 'result': locations,
               'evidence': {'result': locations, 'file_sha256': 'source-digest', 'timestamp': 'now'}}
    result = _capability_from_rpc('LSP references', payload)
    visible = json.loads(result.llm_text.split('\n', 1)[1])
    assert visible['result'] == locations
    assert visible['evidence'] == {'file_sha256': 'source-digest', 'timestamp': 'now'}
    assert result.structured == payload  # Machine consumers retain the original contract.


def test_distinct_evidence_is_not_removed():
    payload = {'status': 'ok', 'result': ['new'], 'evidence': {'result': ['old']}}
    result = _capability_from_rpc('LSP references', payload)
    assert json.loads(result.llm_text.split('\n', 1)[1]) == payload

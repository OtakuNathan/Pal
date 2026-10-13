"""Checks for full-catalog evaluation labels, visibility and metric accounting."""
from scripts.benchmark_full_discovery import build_catalog, diagnose, summarize


def test_catalog_tasks_hide_aliases_and_keep_role_tools_separate():
    declarations, records, cases, generations = build_catalog()
    assert len(declarations) >= len(records) == len(cases)
    resident = generations['resident_union'].search_records
    role = generations['role_union'].search_records
    assert not set(resident).intersection(role)
    assert 'inspect_active_llm' in resident
    assert 'remember_memory' in resident
    assert 'write_provider_memory' not in records
    assert 'add_finding' in role
    assert records['set_browser_checked_state']['domain'] == 'browser'
    assert records['reload_plugin']['domain'] == 'plugin'
    for case in cases:
        assert case['alias'] in generations[case['group']].search_records
        assert case['alias'] not in case['task']


def test_fuzzy_recall_and_errors_are_not_counted_as_strict_success():
    cases = [{'group': 'resident_union'}, {'group': 'role_union'}, {'group': 'resident_union'}]
    rows = [dict(group='resident_union', exact_alias_recall=True, first_recall=True,
                 first_top1=True, strict_recall=True, fuzzy_recall=False),
            dict(group='role_union', exact_alias_recall=True, strict_recall=False,
                 fuzzy_recall=True, first_domain_mismatch=True),
            dict(group='resident_union', exact_alias_recall=True, error='TimeoutError')]
    report = summarize(rows, cases)
    assert report['all']['completed'] == report['all']['expected'] == 3
    assert report['all']['eventual_strict_recall'] == 1
    assert report['all']['eventual_fuzzy_only'] == 1
    assert report['all']['errors'] == 1
    assert report['resident_union']['first_recall'] == 1
    assert report['role_union']['first_domain_mismatch'] == 1


def test_diagnostics_distinguish_vocabulary_from_domain_failures():
    from types import SimpleNamespace
    record = dict(alias='read_file', domain='file', search_objects=('file',), search_terms=('file',))
    records = {'read_file': record}
    generations = {'resident_union': SimpleNamespace(search_records=records)}
    rows = [dict(alias='read_file', group='resident_union', first_recall=False, strict_recall=False,
                 attempts=[dict(query={'domain': 'file', 'query': 'read document'}, hits=[], suggestions=[])])]
    report = diagnose(rows, records, generations)
    assert report['counts']['first_misses'] == {'unmatched_query_words': 1}
    assert report['details'][0]['missing_terms'] == ['document']
    rows[0]['attempts'][0]['query'] = {'domain': 'browser', 'query': 'read file'}
    report = diagnose(rows, records, generations)
    assert report['counts']['first_misses'] == {'wrong_domain': 1}
    assert report['details'][0]['domain_removal_recovers']
    assert not report['domain_boundary_violations']

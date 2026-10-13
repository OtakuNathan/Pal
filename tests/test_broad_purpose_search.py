"""Broad purpose fallback keeps keyword priority, visibility and review evidence."""
from types import SimpleNamespace

import pytest

from pal.core import PalCore
from pal.execution.runtime import ExecutionRuntime


def generation(records):
    return SimpleNamespace(search_records=records)


def test_long_task_description_recalls_after_keyword_stage_miss():
    runtime = generation({"report_defect": {
        "alias": "report_defect", "domain": "bunshin",
        "purpose": "Persist a frozen defect report for contract review.",
    }})
    args = {"query": "submit a frozen defect report for contract revision"}
    assert not ExecutionRuntime._search_keywords(runtime, args)["hits"]
    result = ExecutionRuntime._search_generation(runtime, args)
    hit, = result["hits"]
    assert hit["alias"] == "report_defect"
    assert "submit" in hit["unmatched_terms"]
    assert "revision" in hit["unmatched_terms"]
    assert "not proof" in result["usage_hint"]


def test_one_shared_verb_does_not_return_an_unrelated_callable():
    runtime = generation({"inspect_port": {
        "alias": "inspect_port", "domain": "core", "purpose": "Inspect a connection.",
    }})
    assert not ExecutionRuntime._search_generation(runtime, {"query": "inspect imaginary widget"})["hits"]


@pytest.mark.parametrize('singular,plural', [('dependency', 'dependencies'), ('cache', 'caches')])
def test_purpose_fallback_recalls_noun_number_without_changing_tokenization(singular, plural):
    from pal.execution.discovery_terms import tool_search_terms

    assert tool_search_terms(f'get {plural}') == ('get', plural)
    alias = f'get_{singular}'
    runtime = generation({alias: {'alias': alias, 'purpose': f'Get a {singular}.'}})
    args = {'query': f'get {plural}'}
    assert not ExecutionRuntime._search_keywords(runtime, args)['hits']
    result = ExecutionRuntime._search_generation(runtime, args)
    assert [hit['alias'] for hit in result['hits']] == [alias]


def test_noun_variants_do_not_count_as_two_distinct_positive_matches():
    runtime = generation({'report_defect': {'alias': 'report_defect', 'purpose': 'Persist a report.'}})
    assert not ExecutionRuntime._search_generation(runtime, {'query': 'submit report reports'})['hits']


def test_existing_keyword_matches_and_exact_aliases_are_not_padded():
    runtime = generation({
        "read_file": {"alias": "read_file", "domain": "file", "purpose": "Read file text."},
        "inspect_source": {"alias": "inspect_source", "domain": "lsp", "purpose": "Read file diagnostics."},
    })
    for query in ("read file", "read_file", "READ_FILE"):
        args = {"query": query}
        assert ExecutionRuntime._search_generation(runtime, args) == ExecutionRuntime._search_keywords(runtime, args)


def test_global_purpose_uses_only_visible_tools_and_supplied_filters():
    eligible = {"report_defect": {
        "alias": "report_defect", "domain": "bunshin", "module_id": "bunshin",
        "namespace": "operation", "tags": ["user"],
        "purpose": "Persist a frozen defect report for contract review.",
    }}
    records = {**eligible, "foreign": {
        **eligible["report_defect"], "alias": "foreign", "domain": "plugin", "module_id": "plugins",
    }, "hidden": {**eligible["report_defect"], "alias": "hidden", "namespace": "introspection"}}
    args = {"query": "submit frozen defect report for revision", "module_name": "bunshin",
            "namespace": "action", "tags": ["user"]}
    assert ExecutionRuntime._search_generation(generation(records), args) == ExecutionRuntime._search_generation(generation(eligible), args)
    assert not ExecutionRuntime._search_generation(generation(records), {**args, "tags": ["admin"]})["hits"]
    assert not ExecutionRuntime._search_generation(generation(records), {**args, "query": "foreign"})["hits"]


def test_explicit_owner_miss_never_uses_global_purpose_fallback():
    runtime = generation({"report_defect": {
        "alias": "report_defect", "domain": "bunshin",
        "purpose": "Persist a frozen defect report for contract review.",
    }})
    for owner in ("file", "unknown"):
        result = ExecutionRuntime._search_generation(runtime, {"domain": owner, "query": "frozen defect report"})
        assert not result["hits"] and not result.get("suggestions")


def test_global_fallback_is_bounded_and_independent_of_registry_order():
    records = {f"tool_{index}": {"alias": f"tool_{index}", "domain": "bunshin",
               "purpose": "Persist a frozen defect report for contract review."} for index in range(8)}
    args = {"query": "submit frozen defect report for revision"}
    result = ExecutionRuntime._search_generation(generation(records), args)
    assert [hit["alias"] for hit in result["hits"]] == ["tool_0", "tool_1", "tool_2"]
    assert result == ExecutionRuntime._search_generation(generation(dict(reversed(list(records.items())))), args)
    assert len(ExecutionRuntime._search_generation(generation(records), {**args, "top_k": 100})["hits"]) == 5

"""Full-catalog selection benchmark keeps labels, visibility and recovery honest."""
import asyncio
from types import SimpleNamespace

from scripts.benchmark_full_discovery_flow import build_flow_catalog, resolve_selection, run_case, summarize
from pal.execution.runtime import ExecutionRuntime
from pal.execution.generated_tool_models import ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput as SearchInput
from pal.llm.contracts import generation_result_from_values
from pal.shared.tool_protocol import new_tool_call


def test_full_catalog_contract_modes_and_visibility():
    records,cases,gens=build_flow_catalog()
    positives=[c for c in cases if c['category']=='catalog']
    assert len(positives)==len(records)
    assert len({c['alias'] for c in positives})==len(records)
    assert not set(gens['resident_union'].search_records)&set(gens['role_union'].search_records)
    assert records['list_plugins']['invocation_mode']=='indirect'
    assert records['manage_plugin']['invocation_mode']=='indirect'
    assert records['add_finding']['invocation_mode']=='direct'
    assert 'name' in records['inspect_channel_endpoint']['input_contract']['required']
    assert resolve_selection(gens['resident_union'],'add_finding','direct')['error_code']=='unknown_tool'
    for c in positives:
        assert c['alias'] not in c['task']
        r=records[c['alias']]
        assert resolve_selection(gens[c['group']],c['alias'],r['invocation_mode'])['kind']=='complete'


def test_verification_kind_tasks_are_distinct_and_retrieve_the_right_contract():
    records, cases, generations = build_flow_catalog()
    verifier_cases = [c for c in cases if c['category'] == 'catalog' and c['alias'].startswith('run_verification_')]
    assert len({c['task'] for c in verifier_cases}) == len(verifier_cases)
    generation = generations['role_union']
    for kind, expected in [('contract adversarial', 'run_verification_adversarial_case'),
                           ('unit', 'run_verification_focused_test')]:
        result = ExecutionRuntime._search_generation(generation,
            {'domain': 'bunshin', 'query': f'run {kind} focused tests verification case'})
        assert result['hits'][0]['alias'] == expected
        assert len(result['hits']) <= 3


def test_wrong_mode_returns_real_affordance_without_expected_label():
    _,_,gens=build_flow_catalog()
    reply=resolve_selection(gens['resident_union'],'list_plugins','direct')
    assert reply['error_code']=='wrong_invocation_mode'
    assert reply['effect']=='not_started'
    assert reply['affordances'][0]['tool']=='call_tool'
    assert reply['affordances'][0]['arguments']['name']=='list_plugins'
    assert reply['selection_retry']=={'name':'list_plugins','invocation_mode':'indirect'}


def test_selection_flow_recovers_mode_and_never_has_a_business_handler():
    _,cases,gens=build_flow_catalog()
    case=next(c for c in cases if c['category']=='catalog' and c['alias']=='list_plugins')
    calls=[new_tool_call(name='search_tools',args={'query':'list plugins','domain':'plugin'}),
           new_tool_call(name='select_tool',args={'name':'list_plugins','invocation_mode':'direct'}),
           new_tool_call(name='select_tool',args={'name':'list_plugins','invocation_mode':'indirect'})]
    class Model:
        async def agenerate(self,request):
            assert {t.name for t in request.tools}=={'search_tools','read_tool','select_tool'}
            if len(calls)==1:
                assert request.messages[-1].parts[0].structured['error_code']=='wrong_invocation_mode'
            return generation_result_from_values(tool_calls=[calls.pop(0)],preferred_endpoint_id='fixture',preferred_model_id='fixture')
    row=asyncio.run(run_case(Model(),{'model_id':'fixture','endpoint_id':'fixture'},case,
        gens['resident_union'],ExecutionRuntime._search_generation,SearchInput.model_json_schema(),[]))
    assert row['correct'] and row['first_search_recall']
    assert row['first_selection_correct'] is False
    assert len(row['rejections'])==1


def test_directory_visibility_is_distinct_from_keyword_recall():
    _, cases, generations = build_flow_catalog()
    case = next(c for c in cases if c['category'] == 'catalog' and c['alias'] == 'inspect_llm')
    calls = [new_tool_call(name='search_tools', args={'domain': 'llm'}),
             new_tool_call(name='read_tool', args={'name': case['alias']}),
             new_tool_call(name='select_tool', args={'name': case['alias'], 'invocation_mode': 'indirect'})]
    class Model:
        async def agenerate(self, request):
            return generation_result_from_values(tool_calls=[calls.pop(0)], preferred_endpoint_id='fixture', preferred_model_id='fixture')
    row = asyncio.run(run_case(Model(), {'model_id': 'fixture', 'endpoint_id': 'fixture'}, case,
        generations['resident_union'], ExecutionRuntime._search_generation, SearchInput.model_json_schema(), []))
    assert row['correct'] and row['first_discovery_visible']
    assert not row['first_search_recall']
    assert row['searches'][0]['response_chars'] > 0


def test_fused_contract_selection_needs_no_redundant_read():
    _, cases, generations = build_flow_catalog()
    case = next(c for c in cases if c['category'] == 'catalog' and c['alias'] == 'fill_browser_field')
    calls = [new_tool_call(name='search_tools', args={'domain': 'browser', 'query': 'set input field value'}),
             new_tool_call(name='select_tool', args={'name': case['alias'], 'invocation_mode': 'indirect'})]
    class Model:
        async def agenerate(self, request):
            if len(calls) == 1:
                data = request.messages[-1].parts[0].structured
                assert data['hits'][0]['input_contract']
                assert 'directory' not in data
            return generation_result_from_values(tool_calls=[calls.pop(0)], preferred_endpoint_id='fixture', preferred_model_id='fixture')
    row = asyncio.run(run_case(Model(), {'model_id': 'fixture', 'endpoint_id': 'fixture'}, case,
        generations['resident_union'], ExecutionRuntime._search_generation, SearchInput.model_json_schema(), []))
    assert row['correct'] and len(row['responses']) == 2 and row['reads'] == []
    assert row['first_search_recall']
    assert row['first_contract_recall'] and row['first_contract_top1']
    assert row['first_contract_rr'] == 1
    metrics = summarize([row], [case])['catalog:all']
    assert metrics['first_contract_mrr'] == 1 and metrics['mean_contract_reads'] == 0


def test_valid_wrong_tool_stops_without_correctness_oracle():
    _,cases,gens=build_flow_catalog()
    case=next(c for c in cases if c.get('alias')=='manage_plugin' and c['category']=='catalog')
    class Model:
        async def agenerate(self,request):
            return generation_result_from_values(tool_calls=[new_tool_call(name='select_tool',
                args={'name':'list_plugins','invocation_mode':'indirect'})],preferred_endpoint_id='fixture',preferred_model_id='fixture')
    row=asyncio.run(run_case(Model(),{'model_id':'fixture','endpoint_id':'fixture'},case,
        gens['resident_union'],ExecutionRuntime._search_generation,SearchInput.model_json_schema(),[]))
    assert row['selected_name']=='list_plugins' and not row['correct']
    assert len(row['responses'])==1
    metrics=summarize([row],[case])['catalog:all']
    assert metrics['wrong_selections']==1 and metrics['final_correct']==0


def test_resume_prompt_snapshot_ignores_transport_ids_but_preserves_instructions():
    from scripts.benchmark_full_discovery_flow import manifest_prompt
    from pal.llm.conversions import message_ir_from_dict
    a=message_ir_from_dict({'role':'system','content':'Same rules'})
    b=message_ir_from_dict({'role':'system','content':'Same rules'})
    assert a.message_id != b.message_id
    assert manifest_prompt([a]) == manifest_prompt([b])
    c=message_ir_from_dict({'role':'system','content':'Different rules'})
    assert manifest_prompt([a]) != manifest_prompt([c])


def test_endpoint_failure_without_identity_is_not_model_drift():
    from pal.llm.runtime import _failure_result
    _,cases,gens=build_flow_catalog()
    case=next(c for c in cases if c['category']=='catalog' and c['alias']=='list_plugins')
    class Model:
        async def agenerate(self,request):
            return _failure_result('fixture service unavailable', exc=TimeoutError('fixture timeout'))
    row=asyncio.run(run_case(Model(),{'model_id':'fixture','endpoint_id':'fixture'},case,
        gens['resident_union'],ExecutionRuntime._search_generation,SearchInput.model_json_schema(),[]))
    assert row['error']=='endpoint_failure' and not row['correct']
    assert row['endpoint_failure']['error_type']=='TimeoutError'
    assert len(row['responses'])==1


def test_successful_response_from_other_endpoint_is_still_rejected():
    import pytest
    _,cases,gens=build_flow_catalog()
    case=next(c for c in cases if c['category']=='catalog' and c['alias']=='list_plugins')
    class Model:
        async def agenerate(self,request):
            return generation_result_from_values(text='response',preferred_endpoint_id='other',preferred_model_id='fixture')
    with pytest.raises(RuntimeError,match='Endpoint/model drift'):
        asyncio.run(run_case(Model(),{'model_id':'fixture','endpoint_id':'fixture'},case,
            gens['resident_union'],ExecutionRuntime._search_generation,SearchInput.model_json_schema(),[]))


def test_empty_selection_name_is_a_rejection_instead_of_a_harness_exception():
    _, _, generations = build_flow_catalog()
    for name in ("", "   "):
        result = resolve_selection(generations["resident_union"], name, "indirect")
        assert result["kind"] == "rejected"
        assert result["error_code"] == "invalid_arguments"
        assert result["effect"] == "not_started"

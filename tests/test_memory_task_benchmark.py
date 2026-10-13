"""The task evaluator checks effects and store identity, not just tool names."""
import asyncio
import re

import pytest

from pal.llm.contracts import generation_result_from_values
from pal.shared.tool_protocol import new_tool_call
from scripts.benchmark_memory_tasks import CASES, MemoryFixture, corrected_drink_text, grade, run_case, summarize


@pytest.fixture
def fixture(tmp_path):
    case = next(c for c in CASES if c.goal == 'reconcile')
    fx = MemoryFixture(tmp_path, case)
    try:
        yield case, fx
    finally:
        fx.close()


def test_claiming_success_without_executing_does_not_pass(fixture):
    case, fx = fixture
    result = grade(case, fx, [], '已核实原库的记忆。')
    assert not result['tool_identified']
    assert not result['business_success']


@pytest.mark.parametrize('text,expected', [
    ('用户喜欢红茶。', True),
    ('用户喜欢红茶（此前的咖啡偏好已过时）。', True),
    ('用户现在爱喝红茶，不再喝咖啡。', True),
    ('用户喜欢喝红茶，之前喜欢咖啡，现已改喝红茶。', True),
    ('用户现在爱喝红茶（已从咖啡改为红茶）。', True),
    ('用户从红茶改为咖啡。', False),
    ('用户喜欢咖啡和红茶。', False),
    ('用户不喜欢喝红茶。', False),
    ('用户 饮品 偏好 喜欢 喝 咖啡', False),
])
def test_correction_oracle_distinguishes_old_explanation_from_current_preference(text, expected):
    assert corrected_drink_text(text) is expected


def test_same_ref_in_active_store_does_not_satisfy_original_store_request(fixture):
    case, fx = fixture
    ref = fx.refs['preference']
    result = fx.runtime.invoke_direct_tool(new_tool_call(name='recall_memory', args={'mem_ref': ref}))
    assert result.kind == 'complete'
    graded = grade(case, fx, [{'alias': 'recall_memory', 'args': {'mem_ref': ref},
                             'arguments_valid': True, 'result': result.model_dump(mode='json')}], '已核实。')
    assert graded['tool_identified'] and graded['execution_accepted']
    assert not graded['goal_verified'] and not graded['business_success']


def test_unknown_provider_requires_observed_failure_and_preserves_store(fixture):
    _, fx = fixture
    case = next(c for c in CASES if c.goal == 'unknown_provider')
    assert not grade(case, fx, [], '找不到原库。')['business_success']
    args = {'provider_id': 'missing_l3', 'mem_ref': fx.refs['preference']}
    result = fx.runtime.invoke_direct_tool(new_tool_call(name='recall_memory', args=args))
    assert result.kind == 'failed'
    graded = grade(case, fx, [{'alias': 'recall_memory', 'args': args, 'arguments_valid': True,
                             'result': result.model_dump(mode='json')}], '原库不可用。')
    assert graded['business_success'] and not graded['execution_accepted']


@pytest.mark.parametrize('goal', ['correct', 'forget'])
def test_real_execution_verifies_revision_or_deletion_and_preserves_other_memories(goal):
    case = next(c for c in CASES if c.goal == goal)
    class Model:
        done = False
        async def agenerate(self, request):
            calls = []
            if not self.done:
                task = next(m.text for m in request.messages if m.role.value == 'user')
                ref = re.search(r'fact:fact_[a-z0-9]+', task).group()
                args = {'mem_ref': ref}
                if goal == 'correct':
                    args.update(content='用户喜欢红茶。')
                assert case.target in {tool.name for tool in request.tools}
                calls = [new_tool_call(name=case.target, args=args)]
                self.done = True
            return generation_result_from_values(text='' if calls else '已完成。', tool_calls=calls,
                                                  preferred_endpoint_id='fixture', preferred_model_id='fixture')
    row = asyncio.run(run_case(Model(), {'model_id': 'fixture', 'endpoint_id': 'fixture'},
                               case, 0, {}, 3))
    assert row['tool_identified'] and row['arguments_valid'] and row['execution_accepted']
    assert row['business_success'] and row['constraints_preserved']
    assert summarize([row])['all']['business_success'] == 1


def test_invalid_arguments_are_separate_from_identification_and_business_effects():
    case = next(c for c in CASES if c.goal == 'forget')
    class Model:
        done = False
        async def agenerate(self, request):
            calls = [] if self.done else [new_tool_call(name='call_tool', args={
                'name': 'forget_memory', 'args': {'mem_ref': 123}})]
            self.done = True
            return generation_result_from_values(text='' if calls else '参数错误。', tool_calls=calls,
                                                  preferred_endpoint_id='fixture', preferred_model_id='fixture')
    row = asyncio.run(run_case(Model(), {'model_id': 'fixture', 'endpoint_id': 'fixture'}, case, 0, {}, 3))
    assert row['tool_identified']
    assert not row['arguments_valid'] and not row['execution_accepted'] and not row['business_success']
    assert row['state_before'] == row['state_after']
    assert summarize([row])['all']['argument_rejections'] == 1


def test_memory_endpoint_failure_preserves_trace_and_fixture_state():
    from pal.llm.runtime import _failure_result
    case=next(c for c in CASES if c.goal=='forget')
    class Model:
        async def agenerate(self,request):
            return _failure_result('fixture service unavailable',exc=TimeoutError('fixture timeout'))
    row=asyncio.run(run_case(Model(),{'model_id':'fixture','endpoint_id':'fixture'},case,0,{},3))
    assert row['error']=='endpoint_failure'
    assert row['endpoint_failure']['error_type']=='TimeoutError'
    assert row['state_before']==row['state_after']
    assert not row['business_success'] and len(row['responses'])==1


def test_native_projection_preserves_recursive_contracts_and_literal_values():
    from pal.execution.tool_presentation import native_input_contract
    literal = {"$ref": "literal user data"}
    schema = {"type": "object", "$defs": {"Node": {"type": "object", "properties": {
        "child": {"$ref": "#/$defs/Node"}}}}, "properties": {
        "node": {"$ref": "#/$defs/Node"},
        "value": {"type": "object", "default": literal, "enum": [literal]}}}
    projected = native_input_contract(schema)
    assert projected["properties"]["node"]["type"] == "object"
    assert projected["properties"]["node"]["properties"]["child"]["$ref"] == "#/$defs/Node"
    assert projected["properties"]["value"]["default"] == literal
    assert projected["properties"]["value"]["enum"] == [literal]
    assert schema["properties"]["node"] == {"$ref": "#/$defs/Node"}

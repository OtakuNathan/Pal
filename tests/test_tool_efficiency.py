"""Presentation changes must preserve executable contracts and exact evidence."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from hypothesis import given, strategies as st

from pal.core.runtime import PalCore
from pal.core.turn_executor import TurnExecutor
from pal.execution.capabilities import register_with_core
from pal.execution.contracts import ToolCallBudget
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import InvocationMode, PagingMode, ToolHandlerResult
from pal.llm.ir import GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, WireShape
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.shared.result_rendering import render_structured_for_llm
from pal.shared.tool_protocol import ToolResultIR, default_tool_result_text, new_tool_call
from tests.capability_fixture import mount_test_capability
from tests.test_immutable_tool_facade import _echo_kwargs
from tests import test_tool_search


@given(st.text())
def test_json_compaction_preserves_all_string_data(text):
    obj = {"data": text, "nested": {"output_schema": text}, "json": '{ "a": " b " }'}
    rendered = render_structured_for_llm(obj)
    assert json.loads(rendered) == obj
    assert rendered == json.dumps(obj, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    assert render_structured_for_llm(text) == text


@pytest.mark.parametrize('source', ['    if x:\r\n\tpass  \r\n\r\n', '\n \t ', '{ "a":  1 }\n', '\n--- old\n+++ new\n- x\n+  x\n'])
def test_exact_handler_text_survives_core_bunshin_and_wire_shapes(source):
    runtime = ExecutionRuntime()
    try:
        mount_test_capability(runtime, **_echo_kwargs(mode=InvocationMode.DIRECT, handler=lambda _: ToolHandlerResult(output={'echo': source}, llm_text=source)))
        call = new_tool_call(name='echo', args={'value': 'ok'})
        result = runtime.execute_tool(call)
        assert result.ok
        assert result.structured['echo'] == source
        assert result.llm_text == source
        assert default_tool_result_text(result) == source  # shared Bunshin path
        assert TurnExecutor._render_tool_result_content(None, call, result) == source
        message = LLMMessageIR(role=MessageRole.TOOL, parts=(ToolResultIR(call_id=call.call_id, name='echo', content=source),))
        request = LLMRequestIR(messages=(message,), policy=GenerationPolicyIR(max_output_tokens=100), tools=())
        def strings(value):
            if isinstance(value, str): yield value
            elif isinstance(value, dict):
                for item in value.values(): yield from strings(item)
            elif isinstance(value, (list, tuple)):
                for item in value: yield from strings(item)
        for shape in WireShape:
            wire = codec_for_shape(shape).encode(request, ShapeContext(shape, 'test', 'test-model')).payload
            assert source in list(strings(wire)), shape
    finally:
        runtime.shutdown()


def test_read_tool_full_contract_is_retained_only_in_structured_channel():
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    runtime = core.context.execution_runtime
    try:
        for async_call in (False, True):
            call = new_tool_call(name='read_tool', args={'name': 'run_shell'})
            result = asyncio.run(runtime.execute_tool_async(call)) if async_call else runtime.execute_tool(call)
            visible = json.loads(result.llm_text)
            assert 'output_schema' in result.structured
            assert 'example' in result.structured
            assert 'output_schema' not in visible and 'example' not in visible
            assert visible['input_schema'] == result.structured['input_schema']
            assert 'Output shape:' not in visible['description']
            for retained in ('Failure next steps:', 'Execution semantics:', 'Valid example:'):
                assert retained in visible['description']
        result = runtime.execute_tool(new_tool_call(name='call_tool', args={'name':'exec_tools','args':{}}))
        assert all('output_schema' in item for item in result.structured['tools'])
        assert all('output_schema' not in item for item in json.loads(result.invocation_result.llm_text)['tools'])
    finally:
        runtime.shutdown()


def test_module_filter_alone_applies_in_sync_and_async_facades():
    fixture = test_tool_search.ToolSearchTests()
    fixture.setUp()
    runtime = fixture.core.context.execution_runtime
    try:
        for async_call in (False, True):
            call = new_tool_call(name='search_tools', args={'query':'lookup','module_name':'memory'})
            result = asyncio.run(runtime.execute_tool_async(call)) if async_call else runtime.execute_tool(call)
            assert [x['alias'] for x in result.structured['hits']] == ['memory_lookup']
            assert result.structured['applied_filters']['module_id'] == 'memory'
    finally:
        runtime.shutdown()


@pytest.mark.parametrize('large_internal', [False, True])
def test_paging_uses_visible_text_and_restores_every_character(large_internal):
    runtime = ExecutionRuntime()
    body = 'OK' if large_internal else ('\t x  \r\n' * 800 + '\n \t')
    output = {'echo': 'x' * 9000 if large_internal else 'ok'}
    try:
        mount_test_capability(runtime, **_echo_kwargs(handler=lambda _: ToolHandlerResult(output=output,llm_text=body),paging=PagingMode.SUPPORTED))
        runtime.begin_tool_result_turn(turn_id='t',scope_key='test',input_id='test')
        result = runtime.invoke_indirect_tool(new_tool_call(name='echo',args={'value':'x'},call_id='r'),budget=ToolCallBudget(max_output_chars=500,preview_chars=300),turn_id='t')
        assert result.kind == ('complete' if large_internal else 'paged')
        if not large_internal:
            pages = [runtime.read_tool_result_page(result_ref='r',page=i,turn_id='t') for i in range(1,result.result_handle['page_count']+1)]
            assert ''.join(page.content for page in pages) == body
            assert pages[-1].end_offset == len(body)
    finally:
        runtime.shutdown()

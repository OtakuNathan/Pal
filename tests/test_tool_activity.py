from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from pal.core import PalCore
from pal.core.main_context import MainContext
from pal.execution import register_with_core
from pal.execution.activity import ExecutionActivityDecorator, capture_activity_output, display_arguments
from pal.execution.contracts import ToolCallBudget
from pal.execution.runtime import ExecutionRuntime
from pal.shared import ToolExecutionResult
from pal.shared.tool_protocol import new_tool_call
from pal.channel.tool_activity import ToolActivityRouter


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def test_delegate_results_errors_cancellation_and_bad_sink(self):
        events = []
        decorator = ExecutionActivityDecorator(lambda _: events.append)
        call = new_tool_call(name='run_shell', args={'cmd': 'true'})
        result = ToolExecutionResult(name=call.name, ok=True, text='ok', llm_text='ok')
        count = 0
        async def execute():
            nonlocal count
            count += 1
            capture_activity_output('run_shell', SimpleNamespace(structured={'status':'running','session_id':7}))
            return result
        self.assertIs(await decorator.invoke(call, execute, turn_id='a'), result)
        self.assertEqual(count, 1)
        self.assertEqual([e['status'] for e in events], ['running','background'])
        self.assertEqual(events[-1]['session_id'], 7)
        for error, status in [(ValueError('failure'), 'failed'), (asyncio.CancelledError(), 'cancelled')]:
            async def fail(): raise error
            with self.assertRaises(type(error)):
                await decorator.invoke(call, fail, turn_id='a')
            self.assertEqual(events[-1]['status'], status)
        def broken(_): raise RuntimeError('offline')
        decorator.open_sink = lambda _: broken
        self.assertIs(await decorator.invoke(call, execute, turn_id='a'), result)
        self.assertEqual(count, 2)

    async def test_reused_model_call_ids_do_not_overwrite_activity(self):
        events = []
        decorator = ExecutionActivityDecorator(lambda _: events.append)
        call = new_tool_call(name='read_file', args={}, call_id='call_0')
        result = ToolExecutionResult(name=call.name, ok=True, text='ok', llm_text='ok')
        async def execute():
            return result
        for _ in range(3):
            await decorator.invoke(call, execute, turn_id='same-turn')
        self.assertEqual(len({event['call_id'] for event in events}), 3)
        for start, end in zip(events[::2], events[1::2]):
            self.assertEqual(start['call_id'], end['call_id'])
        self.assertEqual(call.call_id, 'call_0')

    async def test_real_file_calls_diff_before_paging_and_indirect_dedup(self):
        runtime = ExecutionRuntime()
        core = PalCore(context=MainContext(execution_runtime=runtime))
        register_with_core(core.context)
        core.publish_module_capabilities('execution')
        events = []
        runtime.activity_decorator = ExecutionActivityDecorator(lambda _: events.append)
        async def call(name, args, **kwargs):
            return await runtime.execute_tool_async(new_tool_call(name=name,args=args),turn_id='files',**kwargs)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = str(Path(directory)/'file.txt')
                result = await call('write_file',{'file_path':path,'content':'before\n'},budget=ToolCallBudget(max_output_chars=8))
                self.assertTrue(result.ok, result.text)
                self.assertIn('+before', events[-1]['patch'])
                read = await call('read_file',{'file_path':path})
                runtime.commit_tool_delivery(turn_id='files',context_delivery=read.context_delivery,result_id='read-in-l1')
                events.clear()
                result = await call('call_tool',{'name':'edit_file','args':{'file_path':path,'old_string':'before','new_string':'after'}})
                self.assertTrue(result.ok, result.text)
                self.assertEqual(len(events),2)
                self.assertEqual(events[-1]['tool'],'edit_file')
                self.assertIn('-before', events[-1]['patch'])
                self.assertIn('+after', events[-1]['patch'])
                events.clear()
                result = await call('call_tool',{'name':'edit_file','args':{'file_path':path,'old_string':'missing','new_string':'bad'}})
                self.assertFalse(result.ok)
                self.assertEqual(events[-1]['status'],'failed')
                self.assertNotIn('patch',events[-1])
                self.assertEqual(Path(path).read_text(),'after\n')
        finally:
            core.close(); runtime.shutdown()

    def test_display_redacts_only_copy_and_bounds_payload(self):
        args = {'headers':{'Authorization':'bearer secret'},'password':'pw','verification_code':'123456','cmd':'printf hello','nested':{'api_key':'hidden'}}
        text, truncated = display_arguments(args)
        self.assertNotIn('bearer secret',text)
        self.assertNotIn('123456',text)
        self.assertIn('printf hello',text)
        self.assertFalse(truncated)
        self.assertEqual(args['password'],'pw')
        text, truncated = display_arguments({'content':'字'*50000})
        self.assertTrue(truncated)
        self.assertLessEqual(len(text.encode()),8192)

    def test_route_scope_late_events_and_queue_pressure(self):
        avatar = SimpleNamespace(supports_tool_activity=True,enabled=True,attached=True,status_outbox=deque())
        tg = SimpleNamespace(supports_tool_activity=False)
        sent = []
        class Runtime:
            def get_endpoint_hub(self, key): return SimpleNamespace(state="attached")
            def get_endpoint(self, key): return {'avatar':avatar,'tg':tg}.get(key)
            def queue_endpoint_status(self, endpoint, kind, **kwargs): sent.append((endpoint,kind,kwargs))
        router = ToolActivityRouter(Runtime())
        router('turn.start',{'turn_id':'tg-turn','endpoint_id':'tg'})
        self.assertIsNone(router.open_sink('tg-turn'))
        router('turn.start',{'turn_id':'a','endpoint_id':'avatar','reply_target':{'request_id':'original'}})
        sink = router.open_sink('a')
        sink({'action':'call','turn_id':'a','call_id':'1'})
        self.assertEqual(sent[-1][2]['reply_target'],{'request_id':'original'})
        router('turn.end',{'turn_id':'a'})
        size=len(sent)
        sink({'action':'call','turn_id':'a','call_id':'late'})
        self.assertEqual(len(sent),size)
        avatar.attached=False
        router('turn.start',{'turn_id':'b','endpoint_id':'avatar'})
        self.assertEqual(len(sent),size)
        avatar.attached=True
        avatar.status_outbox.extend(SimpleNamespace(kind='other',payload={}) for _ in range(100))
        router('turn.start',{'turn_id':'c','endpoint_id':'avatar'})
        self.assertEqual(len(sent),size)

    async def test_concurrent_capture_isolation_and_diff_limit(self):
        events=[]
        decorator=ExecutionActivityDecorator(lambda _:events.append)
        async def run(turn, marker):
            async def invoke():
                await asyncio.sleep(0)
                capture_activity_output('write_file',SimpleNamespace(structured={'patch':marker*70000}))
                await asyncio.sleep(0)
                return ToolExecutionResult(name='write_file',ok=True,text='ok',llm_text='ok')
            return await decorator.invoke(new_tool_call(name='write_file',args={}),invoke,turn_id=turn)
        await asyncio.gather(run('a','a'),run('b','b'))
        final=[e for e in events if e['status']=='succeeded']
        self.assertEqual(len(final),2)
        for event in final:
            self.assertTrue(event['patch_truncated'])
            self.assertEqual(event['patch'],event['turn_id']*65536)

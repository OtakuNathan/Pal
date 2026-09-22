"""Pal@95373eff product-API regression suite, run against the fix tree.

Adapted from the reviewer's unrun draft (pal_v3_review_95373ef): fixture/API
drift was repaired without weakening any assertion (M03 drives the real
recovery path; M07/M08 bind a live lineage first; M11 is a full-prepare
test).  M16/M18 were added, then M04 (pos/neg through the REAL acceptance
boundary), M05's accept-exception variant, and M06 (late cleanup) joined
as real-path rounds.  Run with PYTHONPATH="$PWD/src:$PWD"; no live
provider calls.
"""
from __future__ import annotations
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
import unittest

from pal.llm import generation_result_from_values
from pal.llm.cache_diagnostics import describe_request, compare_requests
from pal.llm.ir import LLMMessageIR, LLMRequestIR, MessageRole, TextPartIR, GenerationPolicyIR, PromptRegionIR, WireShape, MessageState
from pal.llm.shapes.base import EncodedMessageSpan, EncodedRequest, _JSONFrame
from pal.llm.projection_contracts import (
    AppendReceipt, AttemptKey, EndpointBinding, HistoryCommitReceipt,
    HistoryCursor, LogicalSessionId, OwnerFence, ProjectionSendReceipt,
)
from pal.llm.continuation_policy import NativeCandidate
from pal.llm.projection_session import (
    EndpointProjectionSession, HistoryView,
    _merge_anthropic_user_boundary_pairs, _remap_tail_span,
)
from pal.core.turns import LLMRequestEffect
from pal.shared import PromptAssemblyContext
from pal.memory import MemoryService
from tests.test_v3_n1_root_lifecycle import user, assistant, Candidate
from tests.test_v3_n3_vertical_trace import CapturingTransport, _runtime, _executor, _request_for
from tests.test_v3_af51d74_review_fixes import _continuation


def description(payload, spans=(), messages=None):
    if messages is None:
        messages=[SimpleNamespace(message_id='m', prompt_region=PromptRegionIR.SETTLED_HISTORY)]
    encoded=EncodedRequest(payload=payload,message_spans=tuple(spans))
    return describe_request(SimpleNamespace(messages=messages),encoded,encoded)


class LengthRecoveryTransport:
    """M03 frames: attempt #1 ends with LENGTH, the continuation stops."""

    def __init__(self, fragments):
        self.fragments = list(fragments)
        self.captured = []

    def frames(self, _endpoint, transport_request):
        self.captured.append(transport_request)
        index = len(self.captured) - 1
        payload = {
            "choices": [{
                "message": {"role": "assistant",
                            "content": self.fragments[index] if index < len(self.fragments) else ""},
                "finish_reason": "length" if index == 0 else "stop",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        yield _JSONFrame(0, payload)

    def activate_endpoint(self, endpoint_id):
        pass

    def close(self):
        pass

class ToolCallAnswerTransport:
    """M04 negative frames: the provider answers with a tool call — finalization
    must discard it for fallback text."""

    def __init__(self):
        self.captured = []

    def frames(self, _endpoint, transport_request):
        self.captured.append(transport_request)
        payload = {
            "choices": [{
                "message": {"role": "assistant", "content": "",
                            "tool_calls": [{"id": "call_1", "type": "function",
                                             "function": {"name": "noop", "arguments": "{}"}}]},
                "finish_reason": "tool_calls",
            }],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
        yield _JSONFrame(0, payload)

    def activate_endpoint(self, endpoint_id):
        pass

    def close(self):
        pass


class DiagnosticContracts(unittest.TestCase):
    def test_identical_wire_with_nested_span_coverage_change_is_preserved(self):
        p={'messages':[{'role':'assistant','content':[{'type':'text','text':'SAME'}]}]}
        a=description(p,[EncodedMessageSpan('m',wire_item_paths=(('messages',0,'content',0),))])
        b=description(p)
        self.assertEqual(a['wire_hash'],b['wire_hash'])
        self.assertTrue(compare_requests(a,b)['prefix_preserved'])

    def test_top_level_system_drift_is_not_hidden_by_missing_span(self):
        def p(s):return {'system':[{'type':'text','text':s}], 'messages':[{'role':'user','content':'Q'}]}
        a,b=description(p('OLD')),description(p('NEW'))
        self.assertNotEqual(a['wire_hash'],b['wire_hash'])
        self.assertFalse(compare_requests(a,b)['prefix_preserved'])

    def test_dynamic_subspan_does_not_hide_unspanned_history_in_same_item(self):
        msgs=[SimpleNamespace(message_id='h',prompt_region=PromptRegionIR.SETTLED_HISTORY),
              SimpleNamespace(message_id='d',prompt_region=PromptRegionIR.ACTIVE_DYNAMIC)]
        span=[EncodedMessageSpan('d',wire_item_paths=(('messages',0,'content',1),))]
        def p(h):return {'messages':[{'role':'user','content':[{'type':'text','text':h},{'type':'text','text':'DYNAMIC'}]}]}
        a,b=description(p('OLD'),span,msgs),description(p('NEW'),span,msgs)
        self.assertFalse(compare_requests(a,b)['prefix_preserved'])

class ProjectionSpanContracts(unittest.TestCase):
    def test_continuity_target_is_remapped_with_cache_and_wire_paths(self):
        path=('messages',0,'content',0)
        span=EncodedMessageSpan('summary',cache_targets=(path,),wire_item_paths=(path,),continuity_target=path)
        new=_remap_tail_span(span,container='messages',container_start=3,merged_boundary=None,merged_left_blocks=0)
        self.assertEqual(new.continuity_target,('messages',3,'content',0))
        self.assertEqual(new.continuity_target,new.cache_targets[0])

    def _anthropic_session_with_pending_user(self):
        """A live anthropic lineage whose open tail holds one USER item."""
        session=EndpointProjectionSession(LogicalSessionId('review'))
        session.bind(EndpointBinding(endpoint_id='review-endpoint',model_id='review-model',
                                     wire_shape=WireShape.ANTHROPIC_MESSAGES,
                                     endpoint_spec_revision='rev-1',
                                     continuation_policy_version='policy-1',
                                     config_fingerprint='fp-1'))
        attempt1=AttemptKey(session.identity,OwnerFence(0),'a1')
        session.begin_round(attempt1,requires_native=False)
        session.prepare(HistoryView(cursor=HistoryCursor.initial(),
                                    messages=(LLMMessageIR(role=MessageRole.USER,
                                                           parts=(TextPartIR('pending q'),),
                                                           message_id='pending-q'),)))
        after=HistoryCursor(history_epoch=0,block_sequence=1,prefix_digest='d'*64)
        session.observe_commit(
            HistoryCommitReceipt(attempt=attempt1,
                                 append=AppendReceipt(before=HistoryCursor.initial(),
                                                      after=after,block_count=1),
                                 closed_call_ids=(),native_committed=False),
            span_message_ids=('pending-q',))
        return session,after

    def _prepared_tail(self,tail):
        session,after=self._anthropic_session_with_pending_user()
        attempt=AttemptKey(session.identity,OwnerFence(0),'a2')
        session.begin_round(attempt,requires_native=False)
        return session.prepare(HistoryView(cursor=after,messages=(tail,)))

    def test_no_merge_uses_no_merge_coordinate_transform(self):
        # M11, full prepare: an anthropic pending USER + tail ASSISTANT never
        # merges; the tail span must land on the REAL assistant item, never a
        # merged user coordinate (pending/tail presence is not a merge).
        tail=LLMMessageIR(role=MessageRole.ASSISTANT,parts=(TextPartIR('TAIL'),),
                          message_id='tail-a',state=MessageState.COMPLETE)
        request=self._prepared_tail(tail)
        payload=json.loads(request.payload_json)
        items=payload['messages']
        self.assertEqual([item.get('role') for item in items],['user','assistant'])
        self.assertEqual(items[0]['content'][0]['text'],'pending q')
        self.assertEqual(items[1]['content'][0]['text'],'TAIL')
        span=next(s for s in request.message_spans if s.message_id=='tail-a')
        self.assertEqual(tuple(span.wire_item_paths[0][:2]),('messages',1),
                         'Use the actual merge outcome; a user+assistant boundary '
                         'keeps the assistant item coordinate')
        target=payload
        for part in span.cache_targets[0]:
            target=target[part]
        self.assertEqual(target.get('text'),'TAIL',
                         'the tail cache target must address the real assistant block')

    def test_merged_user_boundary_uses_merged_block_offset(self):
        # The control half of M11: a REAL user+user merge remaps the tail span
        # into the merged item's block coordinates.
        tail=LLMMessageIR(role=MessageRole.USER,parts=(TextPartIR('TAIL-Q'),),message_id='tail-q')
        request=self._prepared_tail(tail)
        payload=json.loads(request.payload_json)
        items=payload['messages']
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['role'],'user')
        self.assertEqual([block['text'] for block in items[0]['content']],['pending q','TAIL-Q'])
        span=next(s for s in request.message_spans if s.message_id=='tail-q')
        self.assertEqual(tuple(span.wire_item_paths[0][:2]),('messages',0),
                         'the merged boundary collapses the tail into item 0')
        target=payload
        for part in span.cache_targets[0]:
            target=target[part]
        self.assertEqual(target.get('text'),'TAIL-Q',
                         'merged tail content must sit after the pending blocks')

    def test_shell_encode_never_scans_the_whole_history(self):
        # M16/S2: the shell envelope encode must see only the preamble; the
        # full effective request a caller passes as the shell must not be
        # encoded end to end (that was O(history) codec work per round).
        import pal.llm.projection_session as ps
        real=ps.codec_for_shape
        calls=[]
        def spy(shape):
            codec=real(shape)
            class _SpyCodec:
                def __getattr__(self,name):return getattr(codec,name)
                def encode(self,request,context):
                    calls.append(len(request.messages))
                    return codec.encode(request,context)
            return _SpyCodec()
        ps.codec_for_shape=spy
        try:
            session=EndpointProjectionSession(LogicalSessionId('review'))
            session.bind(EndpointBinding(endpoint_id='review-endpoint',model_id='review-model',
                                         wire_shape=WireShape.OPENAI_COMPLETION,
                                         endpoint_spec_revision='rev-1',
                                         continuation_policy_version='policy-1',
                                         config_fingerprint='fp-1'))
            attempt=AttemptKey(session.identity,OwnerFence(0),'a1')
            session.begin_round(attempt,requires_native=False)
            history=tuple(LLMMessageIR(role=MessageRole.USER,parts=(TextPartIR(f'history {i}'),),
                                       message_id=f'h{i}') for i in range(40))
            shell=LLMRequestIR(
                messages=(LLMMessageIR(role=MessageRole.SYSTEM,parts=(TextPartIR('BASE'),),message_id='sys'),*history),
                tools=(),policy=GenerationPolicyIR(max_output_tokens=64))
            tail=(LLMMessageIR(role=MessageRole.USER,parts=(TextPartIR('tail'),),message_id='t1'),)
            session.prepare(HistoryView(cursor=HistoryCursor.initial(),messages=tail),request_shell=shell)
        finally:
            ps.codec_for_shape=real
        self.assertTrue(calls)
        self.assertLessEqual(max(calls),2,
                             f'no encode may receive the whole shell history: {calls}')

class RuntimeAndHistoryContracts(unittest.TestCase):
    def test_same_endpoint_profile_drift_invalidates_old_projection(self):
        memory=MemoryService();memory.begin_l1_turn('T',user_message=user('Q','q'))
        runtime=_runtime(CapturingTransport(['A']))
        ex=_executor(memory,runtime)
        pack=ex._prepare_turn_projection(runtime,_request_for(memory,()))
        self.assertIsNotNone(pack)
        endpoint=runtime.active_endpoint()
        endpoint.capabilities_blob={**dict(endpoint.capabilities_blob or {}),'unsupported_request_parameters':['temperature']}
        self.assertIsNone(runtime._projection_for_endpoint(endpoint,pack[0],pack[1]),
                          'Same endpoint id/model/shape is not the same validated profile')
        pack[2]['session'].reject_commit(pack[2]['attempt'].attempt_id,reason='test cleanup')
        runtime.close()

    def test_sync_stale_refresh_recompiles_with_the_new_profile(self):
        # M02: a stale-spec refresh may change the same endpoint's profile.
        # The retry must re-derive the effective request from the REFRESHED
        # config (new output cap) — never re-send the old plan's payload —
        # and the stale projection must drop instead of applying.
        from pal.llm.transport import LLMEndpointSpecStaleError
        memory=MemoryService();memory.begin_l1_turn('T',user_message=user('Q','q'))
        class StaleAndShrinkTransport:
            def __init__(self):self.captured=[];self.raised=False
            def frames(self,_endpoint,transport_request):
                if not self.raised:
                    self.raised=True
                    raise LLMEndpointSpecStaleError('endpoint spec stale')
                self.captured.append(transport_request)
                yield _JSONFrame(0,{'choices':[{'message':{'role':'assistant','content':'OK'},
                                                'finish_reason':'stop'}],
                                    'usage':{'prompt_tokens':5,'completion_tokens':2}})
            def activate_endpoint(self,endpoint_id):pass
            def close(self):pass
        transport=StaleAndShrinkTransport()
        runtime=_runtime(transport)
        ex=_executor(memory,runtime)
        request=_request_for(memory,())
        pack=ex._prepare_turn_projection(runtime,request)
        self.assertIsNotNone(pack)
        encoded,binding,handle=pack
        endpoint=runtime.active_endpoint()
        original_refresh=runtime.refresh_llm_endpoints
        def refresh_and_shrink():
            endpoint.max_output_tokens=64
            return original_refresh()
        runtime.refresh_llm_endpoints=refresh_and_shrink
        result=runtime.generate(
            request,
            projection=encoded,projection_binding=binding,
            projection_attempt_id=handle['attempt'].attempt_id,
            generation_plan=handle['plan'],
        )
        self.assertEqual(len(transport.captured),1,
                         'exactly one provider send after the refresh')
        sent=transport.captured[0].payload
        self.assertEqual(sent.get('max_tokens'),64,
                         'the retry must re-compile with the refreshed output cap')
        receipt=getattr(result,'projection_receipt',None)
        self.assertIsNotNone(receipt)
        self.assertFalse(receipt.applied,
                         'the stale projection must not apply to the refreshed profile')
        runtime.close()

    def test_old_attempt_native_cannot_replace_a_recovered_accepted_message(self):
        # M03, real path: attempt #1 ends LENGTH, the continuation completes
        # the turn, and the REAL runtime recovery merges both fragments.  The
        # receipt must not keep attempt #1's codec capture, and the frozen
        # representation must carry the FULL accepted text.
        memory=MemoryService();memory.begin_l1_turn('T',user_message=user('Q','q'))
        transport=LengthRecoveryTransport(['FIRST_FRAGMENT','RECOVERY_ONLY_SUFFIX'])
        runtime=_runtime(transport)
        ex=_executor(memory,runtime)
        request=_request_for(memory,())
        request=replace(request,metadata={**request.metadata,'max_output_recovery_enabled':True})
        endpoint=runtime.active_endpoint()
        endpoint.capabilities_blob={**dict(endpoint.capabilities_blob or {}),
                                    'max_output_recovery':{'max_continuations':2}}
        pack=ex._prepare_turn_projection(runtime,request)
        self.assertIsNotNone(pack)
        encoded,binding,handle=pack
        result=runtime.generate(
            request,
            projection=encoded,projection_binding=binding,
            projection_attempt_id=handle['attempt'].attempt_id,
            generation_plan=handle['plan'],
        )
        self.assertIn('RECOVERY_ONLY_SUFFIX',str(result.response.message.text),
                      'The accepted turn is the merged recovery output')
        receipt=getattr(result,'projection_receipt',None)
        self.assertIsNotNone(receipt)
        self.assertTrue(receipt.applied,'Same-config projection must still apply')
        self.assertIsNone(getattr(receipt,'native',None),
                          'A recovered contribution cannot carry attempt #1 native')
        memory.upsert_l1_assistant('T',result.response.message)
        ex._observe_turn_projection(pack,SimpleNamespace(turn_id='T'),result)
        session=handle['session']
        if result.response.message.message_id in session.frozen_message_ids():
            self.assertIn('RECOVERY_ONLY_SUFFIX',repr([c.items for c in session.chunks]),
                          'Frozen representation must carry the full accepted text')
        else:
            self.assertIsNone(session._active,'Refusal must leave a recoverable projection, not an abandoned draft')
        runtime.close()

    def test_new_summary_occurs_once_after_real_rebase_and_continuity_view(self):
        memory=MemoryService();root=memory.history_root
        root.begin_right_turn('T',user_text='old question')
        root.stream_right_assistant('T',assistant('old answer','old-a'))
        root.promote(include_active=True)
        marker='SUMMARY_ONE_COPY_95373EF'
        root.begin_compact('test-compact',reason='test')
        root.mark_ready('test-compact',Candidate(summary=marker,rendered=marker))
        root.commit('test-compact')
        memory.append_l1_user('T',user('next question','q-next'))
        runtime=_runtime(CapturingTransport(['unused']));ex=_executor(memory,runtime)
        # A live lineage must exist before the install for the rebase to be
        # real (in production the ordinary rounds ran first).
        runtime.endpoint_projection_session('pal:resident')
        ex._rebase_projection_after_left_install(runtime,'pal:resident',root,memory_service=memory)
        continuity=memory.l1_store.turns.continuity
        self.assertIsNotNone(continuity)
        model_messages=continuity.project_standalone(list(root.right_messages()))
        request=LLMRequestIR(messages=(LLMMessageIR(role=MessageRole.SYSTEM,parts=(TextPartIR('BASE'),)),*model_messages),
                             tools=(),policy=GenerationPolicyIR(max_output_tokens=64),
                             metadata={'prompt_cache_scope_id':'pal:resident'})
        pack=ex._prepare_turn_projection(runtime,request)
        self.assertIsNotNone(pack)
        self.assertEqual(json.dumps(pack[0].payload).count(marker),1,
                         'Raw seed already encoded by rebase + standalone continuity must not duplicate summary')
        pack[2]['session'].reject_commit(pack[2]['attempt'].attempt_id,reason='test cleanup')
        runtime.close()

    def test_consecutive_compacts_share_one_l_identity_mapping(self):
        # M08: the first seed becomes part of the second L.  The canonical
        # mapping (raw source -> standalone reference) stays consistent, the
        # second reseed carries its summary exactly once, and the warm id
        # mapping never compares raw ids against model-view ids.
        memory=MemoryService();root=memory.history_root
        runtime=_runtime(CapturingTransport(['unused']));ex=_executor(memory,runtime)
        runtime.endpoint_projection_session('pal:resident')

        def compact_once(marker):
            root.begin_compact(f'op-{marker}',reason='test')
            root.mark_ready(f'op-{marker}',Candidate(summary=marker,rendered=marker))
            root.commit(f'op-{marker}')
            ex._rebase_projection_after_left_install(runtime,'pal:resident',root,memory_service=memory)

        root.begin_right_turn('T',user_text='old question')
        root.stream_right_assistant('T',assistant('old answer','old-a'))
        root.promote(include_active=True)
        compact_once('GEN_ONE_95373EF')
        memory.append_l1_user('T',user('next question','q-next'))
        memory.upsert_l1_assistant('T',assistant('next answer','a-next'))
        root.promote(include_active=True)
        compact_once('GEN_TWO_95373EF')
        second=memory.l1_store.turns.continuity
        self.assertIsNotNone(second)
        raw_ids=tuple(m.message_id for m in root.left_messages())
        self.assertIn(second.source_id,raw_ids,'the second seed owns the new left')
        model_ids=ex._left_model_view_ids(memory,raw_ids)
        self.assertIn(second.standalone_id,model_ids,
                      'the model view substitutes the standalone reference')
        self.assertNotIn(second.source_id,model_ids)
        model_messages=second.project_standalone(list(root.right_messages()))
        request=LLMRequestIR(messages=(LLMMessageIR(role=MessageRole.SYSTEM,parts=(TextPartIR('BASE'),)),*model_messages),
                             tools=(),policy=GenerationPolicyIR(max_output_tokens=64),
                             metadata={'prompt_cache_scope_id':'pal:resident'})
        pack=ex._prepare_turn_projection(runtime,request)
        self.assertIsNotNone(pack)
        payload=json.dumps(pack[0].payload)
        self.assertEqual(payload.count('GEN_TWO_95373EF'),1,
                         'the second seed must appear exactly once')
        self.assertNotIn('GEN_ONE_95373EF',payload,
                         'the retired first seed must not replay')
        pack[2]['session'].reject_commit(pack[2]['attempt'].attempt_id,reason='test cleanup')
        runtime.close()

    def test_cancelled_ordinary_generation_closes_projection_attempt(self):
        async def scenario():
            memory=MemoryService();memory.begin_l1_turn('T',user_message=user('Q','q'))
            runtime=_runtime(CapturingTransport(['unused']));ex=_executor(memory,runtime)
            request=_request_for(memory,());ex.build_turn_prompt=lambda *a,**k:request
            entered=asyncio.Event()
            async def hanging(*a,**k):
                entered.set();await asyncio.Event().wait()
            runtime.agenerate=hanging
            task=asyncio.create_task(ex._handle_llm_request(LLMRequestEffect(assembly_context=PromptAssemblyContext()),_continuation()))
            try:
                await asyncio.wait_for(entered.wait(),2)
                task.cancel();await asyncio.gather(task,return_exceptions=True)
                session=runtime.endpoint_projection_session('pal:resident',rebind=False)
                self.assertIsNone(session._active,'Interrupted ordinary await must release its own prepared projection')
            finally:
                if not task.done():task.cancel();await asyncio.gather(task,return_exceptions=True)
                runtime.close()
        asyncio.run(scenario())

    def test_replaced_contribution_through_real_boundary_drops_native(self):
        # M04 (negative): finalization_only discards the provider's empty
        # answer and substitutes fallback text — same call ids (none) on both
        # sides.  The pre-boundary native must not survive, and the next
        # request must carry the canonical replacement exactly once.
        memory=MemoryService();memory.begin_l1_turn('T',user_message=user('Q','q'))
        runtime=_runtime(ToolCallAnswerTransport())
        ex=_executor(memory,runtime)
        request=_request_for(memory,());ex.build_turn_prompt=lambda *a,**k:request
        async def scenario():
            return await ex._handle_llm_request(
                LLMRequestEffect(assembly_context=PromptAssemblyContext()),
                _continuation(finalization_only=True))
        result=asyncio.run(scenario())
        self.assertEqual(str(getattr(result.status,'value',result.status)),'ok')
        fallback=str(result.payload.text)
        self.assertIn('text-only final reply',fallback)
        receipt=getattr(result.payload,'projection_receipt',None)
        self.assertIsNotNone(receipt)
        self.assertIsNone(getattr(receipt,'native',None),
                          'a replaced contribution must not keep the provider native')
        session=runtime.endpoint_projection_session('pal:resident',rebind=False)
        self.assertEqual(list(getattr(session,'native_by_attempt',{})),[],
                         'no native may associate with a replaced round')
        next_pack=ex._prepare_turn_projection(runtime,_request_for(memory,()))
        self.assertIsNotNone(next_pack)
        payload=json.dumps(next_pack[0].payload)
        self.assertEqual(payload.count(fallback),1,
                         'the canonical replacement rides the next request exactly once')
        next_pack[2]['session'].reject_commit(next_pack[2]['attempt'].attempt_id,reason='test cleanup')
        runtime.close()

    def test_unmodified_provider_original_freezes_native_byte_true(self):
        # M04 (positive): no rewrite anywhere — the provider original keeps
        # its native representation byte-true through the real accept path.
        memory=MemoryService();memory.begin_l1_turn('T',user_message=user('Q','q'))
        runtime=_runtime(CapturingTransport(['PLAIN_TRUTH_7788']))
        ex=_executor(memory,runtime)
        request=_request_for(memory,());ex.build_turn_prompt=lambda *a,**k:request
        async def scenario():
            return await ex._handle_llm_request(
                LLMRequestEffect(assembly_context=PromptAssemblyContext()),_continuation())
        result=asyncio.run(scenario())
        self.assertEqual(str(result.payload.text),'PLAIN_TRUTH_7788')
        session=runtime.endpoint_projection_session('pal:resident',rebind=False)
        natives=getattr(session,'native_by_attempt',{})
        self.assertEqual(len(natives),1,
                         'an unmodified round freezes exactly its own native')
        stored=next(iter(natives.values()))
        self.assertEqual(json.loads(stored['payload_json']),
                         {'message':{'role':'assistant','content':'PLAIN_TRUTH_7788'}},
                         'the frozen native is the provider wire bytes, verbatim')
        runtime.close()

    def test_failed_l1_acceptance_closes_its_own_draft_and_next_round_prepares(self):
        # M05 (accept-exception variant): the provider answered but L1
        # acceptance raises — the same owner's finally must close the draft,
        # history gains no unaccepted text, and the next round prepares.
        memory=MemoryService();memory.begin_l1_turn('T',user_message=user('Q','q'))
        runtime=_runtime(CapturingTransport(['ANSWER','RETRY_OK']))
        ex=_executor(memory,runtime)
        request=_request_for(memory,());ex.build_turn_prompt=lambda *a,**k:request
        original=ex._upsert_l1_assistant_async
        async def explode(_continuation,_message):raise RuntimeError('L1 down')
        ex._upsert_l1_assistant_async=explode
        async def failing():
            with self.assertRaises(RuntimeError):
                await ex._handle_llm_request(
                    LLMRequestEffect(assembly_context=PromptAssemblyContext()),_continuation())
        asyncio.run(failing())
        ex._upsert_l1_assistant_async=original
        session=runtime.endpoint_projection_session('pal:resident',rebind=False)
        self.assertIsNone(session._active,
                          'a failed acceptance must not strand its draft')
        async def next_round():
            return await ex._handle_llm_request(
                LLMRequestEffect(assembly_context=PromptAssemblyContext()),_continuation())
        result=asyncio.run(next_round())
        self.assertEqual(str(getattr(result.status,'value',result.status)),'ok')
        runtime.close()

    def test_late_cleanup_never_touches_successor_rounds_or_committed_history(self):
        # M06: a LATE finally from attempt A must neither release successor
        # B's open round nor roll back an already-committed A.
        memory=MemoryService();memory.begin_l1_turn('T',user_message=user('Q','q'))
        runtime=_runtime(CapturingTransport(['COMMITTED_A','B_ROUND']))
        ex=_executor(memory,runtime)
        request=_request_for(memory,());ex.build_turn_prompt=lambda *a,**k:request
        captured=[]
        original_prepare=ex._prepare_turn_projection
        def spy_prepare(runtime_arg,req,*a,**k):
            pack=original_prepare(runtime_arg,req,*a,**k)
            if pack is not None:captured.append(pack)
            return pack
        ex._prepare_turn_projection=spy_prepare
        async def round_trip():
            return await ex._handle_llm_request(
                LLMRequestEffect(assembly_context=PromptAssemblyContext()),_continuation())
        first=asyncio.run(round_trip())
        self.assertEqual(str(getattr(first.status,'value',first.status)),'ok')
        pack_a=captured[0]
        session=pack_a[2]['session']
        committed_chunks=len(session.chunks)
        committed_native=dict(getattr(session,'native_by_attempt',{}))
        self.assertGreaterEqual(committed_chunks,1)
        # Late cleanup AFTER the commit must be a no-op: nothing rolls back.
        ex._abandon_turn_projection_round(pack_a)
        self.assertEqual(len(session.chunks),committed_chunks,
                         'a late finally must not roll back a committed round')
        self.assertEqual(dict(getattr(session,'native_by_attempt',{})),committed_native)
        # B opens; A's late cleanup must leave B's round untouched.
        second=asyncio.run(round_trip())
        self.assertEqual(str(getattr(second.status,'value',second.status)),'ok')
        pack_b=captured[-1]
        self.assertNotEqual(pack_a[2]['attempt'].attempt_id,
                            pack_b[2]['attempt'].attempt_id)
        active=getattr(session,'_active',None)
        self.assertIsNone(active,'a completed round leaves no open draft')
        # A's stale pack arriving after B committed: still a no-op.
        ex._abandon_turn_projection_round(pack_a)
        self.assertEqual(len(session.chunks),committed_chunks+1,
                         'B committed its own round; A\'s late cleanup changed nothing')
        runtime.close()

    def test_unconfirmed_local_anchor_is_eligible_until_ttl_expiry(self):
        # M09/S1: local eligibility (submitted write, matching scope/endpoint,
        # TTL fresh) is enough material to build the warm handoff; read
        # confirmation is diagnostics, not a permission gate.
        import time
        from pal.llm.prompt_cache import PromptCacheCoordinator, _AnchorReadEvidence
        coordinator=PromptCacheCoordinator()
        request=LLMRequestIR(
            messages=(LLMMessageIR(role=MessageRole.USER,parts=(TextPartIR('q'),),message_id='q'),),
            tools=(),policy=GenerationPolicyIR(max_output_tokens=64),
            logical_scope_id='pal:resident')
        evidence=_AnchorReadEvidence(
            request=request,anchor_message_id='q',anchor_fingerprint='fp',
            prefix_tokens=100,submitted_sequence=1,submitted_at=time.monotonic(),
            endpoint_id='trace-endpoint',wire_shape='openai_completion',
            dialect='openrouter_openai_explicit',ttl_seconds=300.0)
        coordinator._anchor_evidence['scope']=evidence
        eligible=coordinator.eligible_anchor_request(logical_scope_id='pal:resident',endpoint_id='trace-endpoint')
        self.assertEqual(eligible.get('request'),request,
                         'an unconfirmed local anchor is material for trying')
        self.assertFalse(eligible.get('read_confirmed',True),
                         'no read evidence yet — the flag must say so')
        self.assertEqual(
            coordinator.confirmed_anchor_request(logical_scope_id='pal:resident',endpoint_id='trace-endpoint'),
            {},'the confirmed reader still refuses unconfirmed material')
        evidence.confirmed_at=time.monotonic()
        after=coordinator.eligible_anchor_request(logical_scope_id='pal:resident',endpoint_id='trace-endpoint')
        self.assertTrue(after.get('read_confirmed'))
        self.assertEqual(after.get('request'),request)
        evidence.submitted_at=time.monotonic()-301.0
        self.assertEqual(
            coordinator.eligible_anchor_request(logical_scope_id='pal:resident',endpoint_id='trace-endpoint'),
            {},'a TTL-expired anchor is no longer local material')

    def test_composed_trace_normal_recovery_install_next(self):
        # M18 (composed trace, executor + real root): one ordinary round
        # freezes a chunk, a LENGTH recovery round lands the merged text as
        # the accepted contribution, the install rebases the lineage, and
        # the next request carries the standalone summary exactly once.
        class ComboTransport:
            def __init__(self):self.captured=[]
            def frames(self,_endpoint,transport_request):
                index=len(self.captured);self.captured.append(transport_request)
                content=('A1 answer' if index==0 else 'PART_ONE' if index==1 else 'PART_TWO')
                finish='length' if index==1 else 'stop'
                yield _JSONFrame(0,{'choices':[{'message':{'role':'assistant','content':content},
                                                'finish_reason':finish}],
                                    'usage':{'prompt_tokens':5,'completion_tokens':2}})
            def activate_endpoint(self,endpoint_id):pass
            def close(self):pass
        memory=MemoryService();memory.begin_l1_turn('T',user_message=user('Q','q'))
        transport=ComboTransport()
        runtime=_runtime(transport)
        endpoint=runtime.active_endpoint()
        endpoint.capabilities_blob={**dict(endpoint.capabilities_blob or {}),
                                    'max_output_recovery':{'max_continuations':2}}
        ex=_executor(memory,runtime)
        def build_request(*_a,**_k):
            request=_request_for(memory,())
            return replace(request,metadata={**request.metadata,'max_output_recovery_enabled':True})
        ex.build_turn_prompt=build_request
        continuation=_continuation()
        async def scenario():
            first=await ex._handle_llm_request(
                LLMRequestEffect(assembly_context=PromptAssemblyContext()),continuation)
            self.assertEqual(str(getattr(first.status,'value',first.status)),'ok')
            self.assertIn('A1 answer',str(first.payload.text))
            second=await ex._handle_llm_request(
                LLMRequestEffect(assembly_context=PromptAssemblyContext()),continuation)
            text=str(second.payload.text)
            self.assertIn('PART_ONE',text)
            self.assertIn('PART_TWO',text)
        asyncio.run(scenario())
        session=runtime.endpoint_projection_session('pal:resident',rebind=False)
        self.assertGreaterEqual(len(session.chunks),1,'the normal round must freeze')
        root=memory.history_root
        root.promote(include_active=True)
        root.begin_compact('trace-compact',reason='test')
        root.mark_ready('trace-compact',Candidate(summary='TRACE_SUMMARY_95373EF',rendered='TRACE_SUMMARY_95373EF'))
        root.commit('trace-compact')
        ex._rebase_projection_after_left_install(runtime,'pal:resident',root,memory_service=memory)
        continuity=memory.l1_store.turns.continuity
        self.assertIsNotNone(continuity)
        model_messages=continuity.project_standalone(list(root.right_messages()))
        request=LLMRequestIR(messages=(LLMMessageIR(role=MessageRole.SYSTEM,parts=(TextPartIR('BASE'),)),*model_messages),
                             tools=(),policy=GenerationPolicyIR(max_output_tokens=64),
                             metadata={'prompt_cache_scope_id':'pal:resident'})
        pack=ex._prepare_turn_projection(runtime,request)
        self.assertIsNotNone(pack)
        self.assertEqual(json.dumps(pack[0].payload).count('TRACE_SUMMARY_95373EF'),1,
                         'the standalone summary rides exactly once after the install')
        pack[2]['session'].reject_commit(pack[2]['attempt'].attempt_id,reason='test cleanup')
        runtime.close()

if __name__=='__main__':unittest.main()

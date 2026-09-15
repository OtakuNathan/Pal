import asyncio
from types import SimpleNamespace
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from pal.core import PalCore
from pal.control.contracts import ControlAction, ControlRoute
from pal.execution.native_shell.approval import ShellApprovals, PendingApproval
from pal.execution.native_shell.remote_contract import RemoteFailure


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_request_finishes_on_decision_even_when_card_close_fails(self):
        for choice in ('accept', 'reject', 'invalid', 'cancel'):
            for close_failure in ('error', 'timeout'):
                with self.subTest(choice=choice, close_failure=close_failure):
                    opened = asyncio.Event()
                    binding = SimpleNamespace(
                        response_handle=SimpleNamespace(reply_target={'user_id': 'owner'}),
                        endpoint=SimpleNamespace(endpoint_id='tg', channel_kind='telegram'),
                        control_scope_key='scope', correlation_id='correlation')
                    core = SimpleNamespace(state=SimpleNamespace(active_turns={
                        'turn': SimpleNamespace(delivery_binding=binding)}))
                    approvals = ShellApprovals(SimpleNamespace(core=core))
                    cards = []
                    async def deliver(delivery, **kwargs):
                        cards.append(delivery.interaction)
                        if delivery.interaction.buttons:
                            opened.set()
                            return True
                        if close_failure == 'error':
                            raise RuntimeError('channel offline')
                        await asyncio.Event().wait()
                    core._deliver_control_delivery_async = deliver
                    grant = {'operation_id': 'operation', 'worker_id': 'desktop',
                             'runtime_epoch': 'epoch',
                             'expires_at': datetime.now(timezone.utc).timestamp() + 600}
                    with patch('pal.execution.native_shell.approval.CLOSE_TIMEOUT_SECONDS', 0.01):
                        task = asyncio.create_task(approvals.request('turn', 1,
                            {'action': 'sudo', 'cmd': 'apt update'}, grant))
                        await asyncio.wait_for(opened.wait(), 1)
                        if choice == 'cancel':
                            task.cancel()
                        else:
                            action = ControlAction('shell_privilege_decision', 'execution', cards[0].interaction_id,
                                args={'decision': choice}, route=cards[0].route, trusted_actor='owner')
                            approvals.decide(action)
                        if choice == 'accept':
                            await asyncio.wait_for(task, 1)
                        elif choice == 'cancel':
                            with self.assertRaises(asyncio.CancelledError):
                                await asyncio.wait_for(task, 1)
                        else:
                            with self.assertRaises(RemoteFailure) as caught:
                                await asyncio.wait_for(task, 1)
                            self.assertEqual(caught.exception.code,
                                'approval_rejected' if choice == 'reject' else 'approval_unavailable')
                            self.assertEqual(caught.exception.effect, 'not_started')
                            if choice == 'reject':
                                self.assertEqual(str(caught.exception), 'Approval rejected by user')
                    self.assertFalse(approvals.pending)
                    self.assertFalse(cards[-1].buttons)

    async def test_same_approval_contract_for_tty_telegram_and_avatar(self):
        for channel, reply in [('socket',{'session_id':'owner'}),
                               ('telegram',{'user_id':'owner','chat_id':'100'}),
                               ('desktop_avatar',{'session_id':'owner'})]:
            for choice in ('accept','reject','expire'):
                with self.subTest(channel=channel,choice=choice):
                    deliveries=[]
                    binding=SimpleNamespace(response_handle=SimpleNamespace(reply_target=reply),
                        endpoint=SimpleNamespace(endpoint_id=channel,channel_kind=channel),
                        control_scope_key='scope',correlation_id='correlation')
                    core=SimpleNamespace(state=SimpleNamespace(active_turns={'turn':SimpleNamespace(delivery_binding=binding)}))
                    approvals=ShellApprovals(SimpleNamespace(core=core))
                    async def deliver(delivery,**kwargs):
                        deliveries.append(delivery)
                        spec=delivery.interaction
                        if spec.buttons and choice!='expire':
                            # A different endpoint must never satisfy the original approval.
                            wrong=ControlRoute('another',channel,reply,'scope')
                            approvals.decide(ControlAction('shell_privilege_decision','execution',spec.interaction_id,
                                args={'decision':'accept'},route=wrong,trusted_actor='owner'))
                            self.assertFalse(approvals.pending[spec.interaction_id].future.done())
                            approvals.decide(ControlAction('shell_privilege_decision','execution',spec.interaction_id,
                                args={'decision':choice},route=spec.route,trusted_actor='owner'))
                        return True
                    core._deliver_control_delivery_async=deliver
                    grant={'operation_id':'operation','worker_id':'desktop','runtime_epoch':'epoch',
                           'expires_at':datetime.now(timezone.utc).timestamp()+(0.02 if choice=='expire' else 30)}
                    action=approvals.request('turn',1,{'action':'sudo','cmd':'apt update'},grant)
                    if choice=='accept':await action
                    else:
                        with self.assertRaises(RemoteFailure) as caught:await action
                        self.assertEqual(caught.exception.code,'approval_expired' if choice=='expire' else 'approval_rejected')
                    self.assertEqual(deliveries[0].interaction.route.endpoint_id,channel)
                    self.assertFalse(deliveries[-1].interaction.buttons)
                    self.assertFalse(approvals.pending)
                    self.assertIn({'accept':'Approved once','reject':'Rejected','expire':'expired'}[choice],deliveries[-1].interaction.text)

    async def test_expired_decision_is_rejected_even_before_timeout_callback_runs(self):
        approvals=ShellApprovals(SimpleNamespace(core=None))
        route=ControlRoute('endpoint','socket',{},'scope')
        future=asyncio.get_running_loop().create_future()
        approvals.pending['approval']=PendingApproval(route,'owner',future,0)
        approvals.decide(ControlAction('shell_privilege_decision','execution','approval',
            args={'decision':'accept'},route=route,trusted_actor='owner'))
        self.assertEqual(future.result().code, 'approval_expired')

    async def test_actor_and_route_are_not_model_arguments(self):
        approvals = ShellApprovals(SimpleNamespace(core=None))
        route = ControlRoute('endpoint', 'socket', {'session_id': 'owner'}, 'scope')
        future = asyncio.get_running_loop().create_future()
        approvals.pending['approval'] = PendingApproval(route, 'owner', future)
        def decision(actor='', decision='accept', override=None):
            return ControlAction('shell_privilege_decision', 'execution', 'approval',
                args={'decision': decision, 'actor_id': 'owner', 'confirmed': True},
                route=override or route, trusted_actor=actor)
        approvals.decide(decision())
        self.assertFalse(future.done())
        approvals.decide(decision('intruder'))
        self.assertFalse(future.done())
        approvals.decide(decision('intruder', 'accept_all'))
        self.assertFalse(future.done())
        approvals.decide(decision('owner', override=ControlRoute('elsewhere', 'socket', {}, 'scope')))
        self.assertFalse(future.done())
        approvals.decide(decision('owner'))
        self.assertEqual(future.result(), 'accept')
        approvals.decide(decision('owner', 'reject'))
        self.assertEqual(future.result(), 'accept')

    async def test_embedded_call_cannot_approve_itself(self):
        approvals = ShellApprovals(SimpleNamespace(core=None))
        with self.assertRaises(RemoteFailure) as exc:
            await approvals.request('turn', 1, {'action': 'sudo'}, {})
        self.assertEqual(exc.exception.code, 'approval_unavailable')

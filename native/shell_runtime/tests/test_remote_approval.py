import asyncio
from types import SimpleNamespace
import unittest

from pal.core import PalCore
from pal.control.contracts import ControlAction, ControlRoute
from pal.execution.native_shell.approval import ShellApprovals, PendingApproval
from pal.execution.native_shell.remote_contract import RemoteFailure


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
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
        approvals.decide(decision('owner', 'accept_all'))
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

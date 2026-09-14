"""Resident, single-decision approval on the originating authenticated route."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from pal.control.contracts import ControlRoute, ControlDelivery, InteractionMessageSpec, InteractionButtonSpec
from pal.execution.approval import ExecutionApprovalRequest
from .remote_contract import RemoteFailure


@dataclass
class PendingApproval:
    route: ControlRoute
    actor: str
    future: asyncio.Future


class ShellApprovals:
    def __init__(self, owner):
        self.owner = owner
        self.pending = {}

    async def request(self, turn_id, target, args, approval):
        core = self.owner.core
        continuation = core.state.active_turns.get(turn_id) if core else None
        binding = getattr(continuation, 'delivery_binding', None)
        if binding is None:
            raise RemoteFailure('approval_unavailable', 'A trusted originating conversation is required for privilege approval')
        reply = dict(binding.response_handle.reply_target)
        actor = str(reply.get('user_id') or reply.get('account_id') or reply.get('session_id') or '')
        if not actor:
            raise RemoteFailure('approval_unavailable', 'Channel does not provide a verified human identity')
        route = ControlRoute(binding.endpoint.endpoint_id, binding.endpoint.channel_kind, reply,
                             binding.control_scope_key, binding.correlation_id)
        request = ExecutionApprovalRequest(title=f"Remote {args['action']} on target {target}", risk='high',
            impact=f"Execute exactly this {args['action']} request once on {approval['worker_id']}.",
            approval_kind='remote_privilege', metadata={'target': target, 'operation_id': approval['operation_id']})
        identifier = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[identifier] = PendingApproval(route, actor, future)
        def button(label, decision):
            return InteractionButtonSpec(label, 'control.action.dispatch', {'action_kind': 'shell_privilege_decision',
                'target_scope': 'execution', 'target_id': identifier, 'args': {'decision': decision}})
        text = (f"{request.title}\n{request.impact}\nRuntime: {approval['runtime_epoch']}\n"
                f"Directory: {args.get('cwd') or '(worker default)'}\n\n{args.get('cmd', 'shutdown')}\n\n"
                "Approval applies to this command/script only; it expires in ten minutes.")
        interaction = InteractionMessageSpec(identifier, 'approval_request', route, text,
            ((button('Approve once', 'accept'), button('Reject', 'reject')),),
            datetime.fromtimestamp(approval['expires_at'], timezone.utc).isoformat())
        try:
            delivered = await core._deliver_control_delivery_async(
                ControlDelivery('interactive_open', route, interaction=interaction), require_provider=True)
            if not delivered:
                raise RemoteFailure('approval_unavailable', 'Channel could not deliver the approval request')
            remaining = max(0, approval['expires_at'] - datetime.now(timezone.utc).timestamp())
            try:
                decision = await asyncio.wait_for(future, remaining)
            except TimeoutError:
                raise RemoteFailure('approval_expired', 'No approval was received before expiry')
            if decision != 'accept':
                raise RemoteFailure('approval_rejected', 'User rejected this operation')
        finally:
            self.pending.pop(identifier, None)
            await core._deliver_control_delivery_async(ControlDelivery('interactive_resolve', route,
                interaction=InteractionMessageSpec(identifier, 'approval_request', route, 'Approval request closed.')))

    def decide(self, action):
        pending = self.pending.get(action.target_id)
        route = action.route
        if (pending is None or pending.future.done() or route is None or
            route.endpoint_id != pending.route.endpoint_id or route.control_scope_key != pending.route.control_scope_key or
            action.trusted_actor != pending.actor):
            return {'message': 'Approval is unavailable or this actor/route is not its owner.'}
        if action.args.get('decision') not in {'accept', 'reject'}:
            return {'message': 'Only a single acceptance or rejection is supported.'}
        pending.future.set_result(action.args['decision'])
        return {'message': 'Decision recorded for this operation only.'}

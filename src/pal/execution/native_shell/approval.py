"""Resident, single-decision approval on the originating authenticated route."""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from pal.control.contracts import ControlRoute, ControlDelivery, InteractionMessageSpec, InteractionButtonSpec
from pal.execution.approval import ExecutionApprovalRequest
from .remote_contract import RemoteFailure

logger = logging.getLogger(__name__)
CLOSE_TIMEOUT_SECONDS = 5


@dataclass
class PendingApproval:
    route: ControlRoute
    actor: str
    future: asyncio.Future
    expires_at: float | None = None


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
        self.pending[identifier] = PendingApproval(route, actor, future, approval['expires_at'])
        def button(label, decision):
            return InteractionButtonSpec(label, 'control.action.dispatch', {'action_kind': 'shell_privilege_decision',
                'target_scope': 'execution', 'target_id': identifier, 'args': {'decision': decision}})
        management = args.get('management')
        approved_command = (str(management['action']) + ' ' + ' '.join(management.get('packages', []))) if management else args.get('cmd', 'shutdown')
        text = (f"{request.title}\n{request.impact}\nRuntime: {approval['runtime_epoch']}\n"
                f"Directory: {args.get('cwd') or '(worker default)'}\n\n{approved_command}\n\n"
                "Approval applies to this request only. Execution results are reported separately.")
        interaction = InteractionMessageSpec(identifier, 'approval_request', route, text,
            ((button('Approve once', 'accept'), button('Reject', 'reject')),),
            datetime.fromtimestamp(approval['expires_at'], timezone.utc).isoformat())
        closed_text = 'Approval cancelled or unavailable; no authorization was granted.'
        try:
            delivered = await core._deliver_control_delivery_async(
                ControlDelivery('interactive_open', route, interaction=interaction), require_provider=True)
            if not delivered:
                raise RemoteFailure('approval_unavailable', 'Channel could not deliver the approval request')
            remaining = max(0, approval['expires_at'] - datetime.now(timezone.utc).timestamp())
            try:
                decision = await asyncio.wait_for(future, remaining)
            except TimeoutError:
                closed_text = 'Approval expired; no authorization was granted.'
                raise RemoteFailure('approval_expired', 'No approval was received before expiry')
            if isinstance(decision, RemoteFailure):
                closed_text = str(decision) + '; no authorization was granted.'
                raise decision
            if decision != 'accept':
                closed_text = 'Rejected; no authorization was granted.'
                raise RemoteFailure('approval_rejected', 'Approval rejected by user')
            closed_text = 'Approved once. Execution results are reported separately.'
        except (RemoteFailure, asyncio.CancelledError):
            raise
        except Exception as exc:
            raise RemoteFailure('approval_unavailable', 'Approval could not be completed; no authorization was granted') from exc
        finally:
            self.pending.pop(identifier, None)
            if not future.done():
                future.cancel()
            # Closing the presentation must not replace a rejection/cancellation
            # with a delivery error or hold the tool indefinitely.
            try:
                await asyncio.wait_for(core._deliver_control_delivery_async(ControlDelivery('interactive_resolve', route,
                    interaction=InteractionMessageSpec(identifier, 'approval_request', route, text + '\n\n' + closed_text))),
                    timeout=CLOSE_TIMEOUT_SECONDS)
            except Exception:
                logger.warning('Could not close approval card %s', identifier, exc_info=True)

    def decide(self, action):
        pending = self.pending.get(action.target_id)
        route = action.route
        if (pending is None or pending.future.done() or route is None or
            route.endpoint_id != pending.route.endpoint_id or route.control_scope_key != pending.route.control_scope_key or
            route.channel_kind != pending.route.channel_kind or
            action.trusted_actor != pending.actor):
            return {'message': 'Approval is unavailable or this actor/route is not its owner.'}
        if pending.expires_at is not None and datetime.now(timezone.utc).timestamp() >= pending.expires_at:
            failure = RemoteFailure('approval_expired', 'Approval expired')
            pending.future.set_result(failure)
            return {'message': str(failure)}
        if action.args.get('decision') not in {'accept', 'reject'}:
            failure = RemoteFailure('approval_unavailable', 'Invalid approval decision from the originating channel')
            pending.future.set_result(failure)
            return {'message': str(failure)}
        pending.future.set_result(action.args['decision'])
        return {'message': 'Decision recorded for this operation only.'}

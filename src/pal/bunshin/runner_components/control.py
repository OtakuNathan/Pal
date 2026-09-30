from __future__ import annotations
from pal.bunshin.runner_components.models import _BunshinCooperativeCancel
from pal.bunshin.runner_components.models import _BunshinCooperativeRestart
from pal.bunshin.runner_components.models import DecisionReader
from pal.shared.tool_protocol import ToolCallIR
from dataclasses import dataclass, field
from typing import Any
from pal.execution import ExecutionApprovalRequest
from pal.bunshin.user_interaction import BunshinUserInteractionPort
from pal.shared import BunshinInvocationPack
from pal.bunshin.runner_components.reporter import Reporter


@dataclass
class Control:
    reporter: Reporter
    bunshin_id: str
    pack: BunshinInvocationPack
    read_decision: DecisionReader
    run_id: str
    auto_accept_approvals: bool = False
    user_interaction: BunshinUserInteractionPort | None = field(default=None, init=False, repr=False)
    pending_control_messages: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    cancel_requested: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    restart_requested: dict[str, Any] = field(default_factory=dict, init=False, repr=False)

    async def read_manager_control(self, timeout: float | None = None) -> dict[str, Any] | None:
        if self.pending_control_messages:
            message = self.pending_control_messages.pop(0)
        else:
            message = await self.read_decision(timeout)
        if not message:
            return None
        message_type = str(message.get("type") or "").strip()
        if message_type in {"cancel_requested", "cancel"}:
            raise _BunshinCooperativeCancel(self.remember_cancel_request(dict(message.get("payload") or message)))
        if message_type in {"restart_requested", "manager_restart"}:
            raise _BunshinCooperativeRestart(
                self.remember_restart_request(dict(message.get("payload") or message))
            )
        return message

    async def poll_cancel_requested(self) -> dict[str, Any]:
        if self.cancel_requested:
            return dict(self.cancel_requested)
        while True:
            message = await self.read_decision(0.001)
            if not message:
                return {}
            message_type = str(message.get("type") or "").strip()
            if message_type in {"cancel_requested", "cancel"}:
                return self.remember_cancel_request(dict(message.get("payload") or message))
            if message_type in {"restart_requested", "manager_restart"}:
                self.remember_restart_request(dict(message.get("payload") or message))
                continue
            self.pending_control_messages.append(dict(message))
            return {}

    async def raise_if_cancel_requested(self) -> None:
        cancel = await self.poll_cancel_requested()
        if cancel:
            raise _BunshinCooperativeCancel(cancel)

    async def raise_if_restart_requested(self) -> None:
        if self.restart_requested:
            raise _BunshinCooperativeRestart(dict(self.restart_requested))

    def remember_cancel_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.cancel_requested:
            return dict(self.cancel_requested)
        reason = str(payload.get("reason") or payload.get("summary") or "cooperative_cancel_requested").strip()
        summary = str(payload.get("summary") or "bunshin cancellation requested").strip()
        self.cancel_requested = {
            **dict(payload),
            "reason": reason,
            "summary": summary,
            "status": str(payload.get("status") or "killed"),
        }
        return dict(self.cancel_requested)

    def remember_restart_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.restart_requested:
            return dict(self.restart_requested)
        self.restart_requested = {
            **dict(payload),
            "reason": str(payload.get("reason") or "manager_restart_requested"),
            "summary": str(
                payload.get("summary")
                or "bunshin suspended after the current durable LLM/tool safe point"
            ),
        }
        return dict(self.restart_requested)

    async def request_execution_approval(
        self,
        request: ExecutionApprovalRequest,
        tool_call: ToolCallIR,
        *,
        capability_name: str,
    ) -> str:
        return await self.request_approval(
            capability_name,
            tool_call,
            approval_kind=request.approval_kind,
            title=request.title,
            risk=request.risk,
            impact=request.impact,
            metadata=request.metadata,
        )

    async def request_approval(
        self,
        capability_name: str,
        tool_call: ToolCallIR,
        *,
        approval_kind: str = "high_risk",
        title: str | None = None,
        risk: str = "high",
        impact: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        decision = await self.user_interaction_port().request_approval(
            capability_name=capability_name,
            args_summary=dict(tool_call.args),
            approval_policy=self.pack.approval_policy or {},
            approval_kind=approval_kind,
            title=title,
            risk=risk,
            impact=impact,
            metadata=metadata,
        )
        self.auto_accept_approvals = self.user_interaction_port().auto_accept_approvals
        return decision

    def user_interaction_port(self) -> BunshinUserInteractionPort:
        if self.user_interaction is None:
            binding = dict(dict(self.pack.metadata or {}).get("bunshin_v2") or {})
            self.user_interaction = BunshinUserInteractionPort(
                emit_event=self.reporter.emit,
                read_response=self.read_manager_control,
                run_id=self.run_id,
                bunshin_id=self.bunshin_id,
                invocation_id=self.pack.invocation_id,
                workflow_id=str(binding.get("workflow_id") or ""),
                auto_accept_approvals=self.auto_accept_approvals,
            )
        return self.user_interaction

    async def request_architecture_clarification(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return await self.user_interaction_port().request_clarification(payload)

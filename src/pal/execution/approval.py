from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult


@dataclass(frozen=True)
class ExecutionApprovalRequest:
    title: str
    risk: str
    impact: str
    approval_kind: str = "high_risk"
    metadata: dict[str, Any] = field(default_factory=dict)


ApprovalClassifier = Callable[
    [CanonicalToolCall],
    ExecutionApprovalRequest | None | Awaitable[ExecutionApprovalRequest | None],
]
ApprovalRequester = Callable[
    [ExecutionApprovalRequest, CanonicalToolCall],
    str | Awaitable[str],
]


@dataclass
class ApprovalExecutionDecorator:
    delegate: Any
    classify: ApprovalClassifier
    request: ApprovalRequester

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)

    async def execute_tool_async(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        budget: Any = None,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
        request = self.classify(call)
        if inspect.isawaitable(request):
            request = await request
        if request is not None:
            decision = self.request(request, call)
            if inspect.isawaitable(decision):
                decision = await decision
            normalized = str(decision or "timeout").strip().lower()
            if normalized != "accept":
                text = f"approval {normalized}"
                return CanonicalToolResult(
                    name=call.name,
                    ok=False,
                    text=text,
                    structured={
                        "reason": "approval_not_accepted",
                        "decision": normalized,
                        "capability": call.name,
                        "risk": request.risk,
                    },
                    call_id=call.call_id,
                    llm_text=text,
                    status="error",
                )
        execute = getattr(self.delegate, "execute_tool_async")
        return await execute(
            call,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id,
        )

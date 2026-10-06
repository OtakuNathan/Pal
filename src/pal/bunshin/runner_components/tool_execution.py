from __future__ import annotations
from pal.bunshin.runner_components.prompt_values import _provider_call_with_effective_args
from pal.shared.tool_protocol import ToolCallIR, new_tool_call
from dataclasses import dataclass
from typing import Any
from pal.execution import ApprovalExecutionDecorator, ExecutionApprovalRequest
from pal.bunshin.profiles import filter_bunshin_allowed_capabilities
from pal.bunshin.scoped_execution import _effective_capability_name, _effective_tool_args
from pal.bunshin.tool_admission import admit_bunshin_tool_call
from pal.shared import ToolExecutionResult, BunshinInvocationPack
from pal.bunshin.runner_components.control import Control
from pal.bunshin.runner_components.research_budget import ResearchBudget
from pal.bunshin.runner_components.review_evidence import ReviewEvidence
from pal.bunshin.runner_components.status import Status
from pal.bunshin.runner_components.tool_session import ToolSession


@dataclass
class ToolExecution:
    control: Control
    research_budget: ResearchBudget
    review_evidence: ReviewEvidence
    status: Status
    tool_session: ToolSession
    pack: BunshinInvocationPack
    run_id: str

    async def execute_allowed_tool(
        self,
        execution_runtime: "BunshinScopedExecutionRuntime",
        tool_call: ToolCallIR,
        *,
        allow_tools: bool = True,
        budget: Any = None,
        turn_id: str | None = None,
    ) -> ToolExecutionResult:
        effective_allowed = getattr(execution_runtime, "allowed_capabilities", None) or self.pack.allowed_capabilities
        allowed_items = [
            str(item).strip()
            for item in [*list(self.pack.allowed_capabilities or []), *list(effective_allowed or [])]
            if str(item).strip()
        ]
        allowed_items = filter_bunshin_allowed_capabilities(allowed_items)
        resolve_name = getattr(execution_runtime, "resolve_capability_address", None)
        if not callable(resolve_name):
            resolve_name = lambda name: str(name or "").strip()
        provider_call = tool_call
        admission = admit_bunshin_tool_call(
            provider_call,
            allowed_items,
            resolve_name=resolve_name,
            require_effective_target=True,
        )
        policy_call = self.tool_call_with_bunshin_defaults(admission.call)
        tool_call = _provider_call_with_effective_args(provider_call, policy_call)
        target_name = admission.target_name
        if not admission.ok:
            self.status.block(f"{admission.message}: {target_name}")
            return admission.to_result()
        delegate = execution_runtime
        if self.tool_session.execution_sessions is not None:
            if self.tool_session.execution_sessions.handles_capability(target_name):
                # Approval and cancellation share the Manager control reader.
                # Start the cancellation watcher only after approval settles.
                delegate = self.tool_session.execution_sessions.execution_delegate(execution_runtime, self.control.raise_if_cancel_requested)
        approval_runtime = ApprovalExecutionDecorator(
            delegate=delegate,
            classify=lambda _call: self.execution_approval_request(target_name),
            request=lambda request, call: self.control.request_execution_approval(
                request,
                call,
                capability_name=target_name,
            ),
        )
        from pal.bunshin.v2.verification_readiness import (
            record_verification_execution, verification_corpus_snapshot,
        )
        evidence_workspace = dict(getattr(execution_runtime, "workspace", self.pack.workspace) or {})
        bound_execution = (
            dict(evidence_workspace.get("bunshin_v2") or {}).get("role") == "verifier"
            and (target_name == "op_exec_shell" or target_name.startswith("op_lsp_"))
        )
        before = verification_corpus_snapshot(evidence_workspace) if bound_execution else None
        result = await approval_runtime.execute_tool_async(
            tool_call,
            allow_tools=allow_tools,
            budget=budget,
            turn_id=turn_id or self.run_id,
        )
        if (result.structured or {}).get("reason") == "approval_not_accepted":
            decision = str((result.structured or {}).get("decision") or "timeout")
            self.status.block(f"approval {decision} for {target_name}")
            result.structured["capability"] = target_name
        self.research_budget.record_web_research_usage(target_name)
        if before is not None:
            evidence_workspace["review_tool_evidence_refs"] = self.review_evidence.review_tool_evidence_refs
            record_verification_execution(evidence_workspace, new_tool_call(
                name=target_name, args=_effective_tool_args(policy_call), call_id=policy_call.call_id,
            ), result, before)
        else:
            self.review_evidence.record_review_tool_evidence(target_name, policy_call, result)
        return result

    def tool_call_with_bunshin_defaults(self, tool_call: ToolCallIR) -> ToolCallIR:
        target_name = _effective_capability_name(tool_call)
        if str(target_name).startswith("op_lsp_"):
            return self.tool_call_with_lsp_workspace(tool_call)
        if str(target_name) == "op_exec_shell":
            return self.tool_call_with_shell_workspace(tool_call)
        return tool_call

    def tool_call_with_shell_workspace(self, tool_call: ToolCallIR) -> ToolCallIR:
        effective_args = _effective_tool_args(tool_call)
        workspace = dict(self.pack.workspace or {})
        repo_path = str(
            workspace.get("repo_path")
            or workspace.get("workspace_path")
            or workspace.get("task_repo_path")
            or workspace.get("target_repo_path")
            or ""
        ).strip()
        if repo_path and not str(effective_args.get("cwd") or "").strip():
            effective_args["cwd"] = repo_path
        if tool_call.name == "op_tool_call":
            args = dict(tool_call.args or {})
            args["args"] = effective_args
            return new_tool_call(name=tool_call.name, args=args, call_id=tool_call.call_id)
        return new_tool_call(name=tool_call.name, args=effective_args, call_id=tool_call.call_id)

    def tool_call_with_lsp_workspace(self, tool_call: ToolCallIR) -> ToolCallIR:
        effective_args = _effective_tool_args(tool_call)
        workspace = dict(self.pack.workspace or {})
        repo_path = str(
            workspace.get("repo_path")
            or workspace.get("workspace_path")
            or workspace.get("task_repo_path")
            or workspace.get("target_repo_path")
            or ""
        ).strip()
        if repo_path:
            effective_args["workspace_root"] = repo_path
        if tool_call.name == "op_tool_call":
            args = dict(tool_call.args or {})
            args["args"] = effective_args
            return new_tool_call(name=tool_call.name, args=args, call_id=tool_call.call_id)
        return new_tool_call(name=tool_call.name, args=effective_args, call_id=tool_call.call_id)

    def execution_approval_request(self, capability_name: str) -> ExecutionApprovalRequest | None:
        if self.control.user_interaction_port().should_request_approval(
            capability_name,
            self.pack.approval_policy or {},
        ):
            return ExecutionApprovalRequest(
                title="Bunshin high-risk operation",
                risk="high",
                impact="Bunshin requested permission before running a high-risk operation.",
            )
        if self.control.auto_accept_approvals:
            return None
        status = self.research_budget.web_research_budget_status(capability_name)
        if not status or int(status["used"]) < int(status["budget"]):
            return None
        return ExecutionApprovalRequest(
            title="Bunshin web research budget",
            risk="medium",
            impact="Bunshin used the included web research budget for this run and requested permission before another web call.",
            approval_kind="web_research_budget",
            metadata={
                "web_research_budget": status["budget"],
                "web_research_used": status["used"],
                "web_research_budget_key": str(status.get("key") or ""),
            },
        )

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import mimetypes
import os
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

from pal.artifact import ArtifactManager, ArtifactRepository, register_with_core as register_artifact_with_core
from pal.core import PalCore
from pal.core.runtime_config import RuntimeConfig
from pal.execution import CapabilityCall, CapabilityResult, register_with_core as register_execution_with_core
from pal.foundation import PalV2Database, utc_now
from pal.llm import EndpointResolver, LLMEndpointRepository, LLMRuntime, LiteLLMCredentialResolver, RuntimeSettingRepository, build_default_endpoint_invoker
from pal.llm.contracts import CanonicalLLMRequest, CanonicalToolCall, CanonicalToolResult
from pal.llm.secret_store import EncryptedFileSecretStore
from pal.memory import L3ProviderSelector, MemoryService, register_with_core as register_memory_with_core
from pal.minion.git_env import commit_milestone
from pal.minion.profiles import filter_minion_allowed_capabilities, is_minion_capability_denied
from pal.plugins.l3 import SQLiteVecL3Plugin, register_with_core as register_l3_with_core
from pal.shared import LLMFinishReason, RuntimeStatus, TaskContextPack
from pal.execution.tool_search import ToolReadTool, ToolSearchTool
from pal.web_fetch import BrowserServiceManager, WebFetchProviderRepository, WebFetchService, register_with_core as register_web_fetch_with_core
from pal.web_search import WebSearchProviderRepository, WebSearchService, register_with_core as register_web_search_with_core
from pal.wizard.runtime import ALL_MODELS, DEFAULT_LLM_ENDPOINTS, DEFAULT_WEB_FETCH_PROVIDERS, DEFAULT_WEB_SEARCH_PROVIDERS


EventWriter = Callable[[dict[str, Any]], Awaitable[None]]
DecisionReader = Callable[[float | None], Awaitable[dict[str, Any] | None]]


@dataclass
class MinionRuntimeBundle:
    llm_runtime: Any
    execution_runtime: Any
    close_async: Callable[[], Awaitable[None]] | None = None

    async def close(self) -> None:
        if self.close_async is not None:
            await self.close_async()


@dataclass
class MinionRunner:
    runtime_root: Path
    pack: TaskContextPack
    minion_id: str
    run_id: str
    write_event: EventWriter
    read_decision: DecisionReader
    runtime_bundle: MinionRuntimeBundle | None = None
    blocked_summary: str = ""
    produced_artifacts: list[dict[str, Any]] = field(default_factory=list)

    async def run(self) -> int:
        bundle: MinionRuntimeBundle | None = None
        self._append_debug_log(
            "runner_started",
            {
                "goal": self.pack.goal,
                "instruction": self.pack.instruction,
                "allowed_capabilities": list(self.pack.allowed_capabilities),
            },
        )
        try:
            bundle = self.runtime_bundle or build_slim_minion_runtime(self.runtime_root)
            await self._emit("phase_started", {"phase": "accepted", "summary": "minion accepted task context", "prompt_scaffold": self._prompt_scaffold()})
            await self._emit("phase_started", {"phase": "milestone_started", "summary": self._current_milestone_title()})
            final_text = await self._llm_tool_loop(bundle)
            if self.blocked_summary:
                terminal_payload = self._terminal_payload("blocked", self.blocked_summary)
                await self._emit(
                    "checkpoint",
                    {"status": "blocked", "milestone_index": self._current_milestone_index(), "summary": self.blocked_summary},
                )
                await self._emit("terminal", terminal_payload)
                return 0
            checkpoint_payload = await self._complete_current_milestone(final_text)
            if checkpoint_payload.get("status") != "completed":
                await self._emit("checkpoint", checkpoint_payload)
                await self._emit("terminal", self._terminal_payload("blocked", checkpoint_payload.get("summary") or "milestone blocked"))
                return 0
            await self._emit("checkpoint", checkpoint_payload)
            await self._emit("terminal", self._terminal_payload("completed", final_text or "minion completed current milestone"))
            return 0
        except Exception as exc:
            with contextlib.suppress(Exception):
                await self._emit(
                    "terminal",
                    {
                        "status": "failed",
                        "summary": f"minion runner failed: {exc.__class__.__name__}",
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                        "task_lessons": [],
                        "system_lessons": [],
                    },
                )
            return 1
        finally:
            if bundle is not None:
                await bundle.close()
            self._append_debug_log("runner_stopped", {"blocked_summary": self.blocked_summary})

    async def _llm_tool_loop(self, bundle: MinionRuntimeBundle) -> str:
        messages = self._initial_messages()
        execution_runtime = MinionScopedExecutionRuntime(
            bundle.execution_runtime,
            self.pack.allowed_capabilities,
            dict(self.pack.workspace),
            produced_artifacts=self.produced_artifacts,
        )
        tools = _llm_tools_for_allowed(execution_runtime, self.pack.allowed_capabilities)
        max_rounds = _optional_positive_int(self.pack.metadata.get("max_tool_rounds") if isinstance(self.pack.metadata, dict) else None)
        max_output_tokens = _resolve_minion_max_output_tokens(bundle.llm_runtime, self.pack)
        final_text = ""
        tool_call_count = 0
        nudged_for_tool = False
        rounds = 0
        while True:
            if max_rounds is not None and rounds >= max_rounds:
                if self._completion_evidence_present():
                    return final_text or "milestone produced completion evidence"
                self.blocked_summary = f"minion reached explicit max_tool_rounds={max_rounds} before completing the current milestone"
                return final_text
            rounds += 1
            await self._emit_progress(
                "llm_round_started",
                round=rounds,
                tool_call_count=tool_call_count,
                tool_count=len(tools),
            )
            request = CanonicalLLMRequest(
                messages=list(messages),
                max_output_tokens=max_output_tokens,
                tools=list(tools),
                metadata=_minion_llm_request_metadata(self.pack, self.run_id),
            )
            self._append_debug_log(
                "llm_request",
                {
                    "round": rounds,
                    "messages": request.messages,
                    "tools": request.tools,
                    "metadata": request.metadata,
                },
            )
            outcome = await self._await_with_progress_heartbeat(
                bundle.llm_runtime.agenerate(request),
                phase="llm_round_waiting",
                round=rounds,
                tool_call_count=tool_call_count,
            )
            final_text = str(getattr(outcome, "text", "") or "").strip()
            finish_reason = str(getattr(outcome, "finish_reason", "") or "").strip()
            self._append_debug_log(
                "llm_outcome",
                {
                    "round": rounds,
                    "finish_reason": finish_reason,
                    "response_mode": str(getattr(outcome, "response_mode", "") or ""),
                    "tool_calls": [_tool_call_summary(item) for item in list(getattr(outcome, "tool_calls", []) or [])],
                    "reasoning_text": str(getattr(outcome, "reasoning_text", "") or ""),
                    "text": final_text,
                },
            )
            if finish_reason == LLMFinishReason.ERROR:
                self.blocked_summary = final_text or "LLM generation failed"
                return final_text
            if finish_reason == LLMFinishReason.COMPACT_REQUIRED:
                self.blocked_summary = "minion LLM context requires compaction before continuing"
                return final_text
            tool_calls = [self._ensure_tool_call_identity(item) for item in list(getattr(outcome, "tool_calls", []) or [])]
            await self._emit_progress(
                "llm_round_completed",
                round=rounds,
                finish_reason=finish_reason,
                tool_call_count=tool_call_count,
                tool_calls=[_tool_call_summary(item) for item in tool_calls],
                text_preview=_preview_text(final_text),
            )
            if not tool_calls:
                if tools and tool_call_count == 0 and not nudged_for_tool and self._requires_first_tool_call():
                    nudged_for_tool = True
                    messages.append({"role": "assistant", "content": final_text})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "You have not used any capability yet. This milestone requires executable evidence. "
                                "Use one listed capability now to inspect, research, read, write, or verify the task before completing."
                            ),
                        }
                    )
                    continue
                await self._emit_progress("milestone_finalizing", round=rounds, tool_call_count=tool_call_count)
                return final_text
            messages.append(_assistant_tool_message(final_text, tool_calls))
            for index, tool_call in enumerate(tool_calls):
                target_name = _effective_capability_name(tool_call)
                await self._emit_progress(
                    "tool_call_started",
                    round=rounds,
                    tool_call_index=index,
                    tool_name=tool_call.name,
                    target_name=target_name,
                    args_preview=_json_preview(tool_call.args),
                )
                try:
                    self._append_debug_log(
                        "tool_call_started",
                        {
                            "round": rounds,
                            "tool_call_index": index,
                            "tool_name": tool_call.name,
                            "target_name": target_name,
                            "args": dict(tool_call.args),
                        },
                    )
                    result = await self._await_with_progress_heartbeat(
                        self._execute_allowed_tool(execution_runtime, tool_call),
                        phase="tool_call_waiting",
                        round=rounds,
                        tool_call_index=index,
                        tool_name=tool_call.name,
                        target_name=target_name,
                    )
                except Exception as exc:
                    self._append_debug_log(
                        "tool_call_failed",
                        {
                            "round": rounds,
                            "tool_call_index": index,
                            "tool_name": tool_call.name,
                            "target_name": target_name,
                            "error_type": exc.__class__.__name__,
                            "error": str(exc),
                        },
                    )
                    await self._emit_progress(
                        "tool_call_failed",
                        round=rounds,
                        tool_call_index=index,
                        tool_name=tool_call.name,
                        target_name=target_name,
                        error_type=exc.__class__.__name__,
                        error=_preview_text(str(exc), limit=500),
                    )
                    raise
                tool_call_count += 1
                self._append_debug_log(
                    "tool_call_completed",
                    {
                        "round": rounds,
                        "tool_call_index": index,
                        "tool_name": tool_call.name,
                        "target_name": target_name,
                        "ok": bool(result.ok),
                        "status": str(result.status or ""),
                        "text": _tool_result_text(result),
                        "structured": dict(result.structured or {}),
                    },
                )
                await self._emit_progress(
                    "tool_call_completed",
                    round=rounds,
                    tool_call_index=index,
                    tool_name=tool_call.name,
                    target_name=target_name,
                    ok=bool(result.ok),
                    status=str(result.status or ""),
                    text_preview=_preview_text(_tool_result_text(result)),
                )
                messages.append({"role": "tool", "tool_call_id": str(tool_call.call_id or ""), "content": _tool_result_text(result)})
                if self.blocked_summary:
                    return final_text or self.blocked_summary

    def _requires_first_tool_call(self) -> bool:
        if bool((self.pack.metadata or {}).get("allow_text_only_completion")):
            return False
        completion_policy = self._completion_policy()
        if "requires_capability_evidence" in completion_policy:
            return bool(completion_policy.get("requires_capability_evidence")) and bool(self.pack.allowed_capabilities)
        return str(completion_policy.get("evidence") or "").strip().lower() == "git_commit" and bool(self.pack.allowed_capabilities)

    async def _execute_allowed_tool(self, execution_runtime: "MinionScopedExecutionRuntime", tool_call: CanonicalToolCall) -> CanonicalToolResult:
        target_name = _effective_capability_name(tool_call)
        allowed = set(str(item) for item in self.pack.allowed_capabilities)
        if is_minion_capability_denied(tool_call.name) or is_minion_capability_denied(target_name):
            self.blocked_summary = f"capability is denied by minion policy: {target_name}"
            return CanonicalToolResult(
                name=tool_call.name,
                ok=False,
                text="capability is denied by minion policy",
                structured={"reason": "capability_denied_by_minion_policy", "capability": target_name},
                call_id=tool_call.call_id,
                llm_text="capability is denied by minion policy",
                status=RuntimeStatus.ERROR,
            )
        if tool_call.name not in allowed or target_name not in allowed:
            self.blocked_summary = f"capability is not allowed for this minion run: {target_name}"
            return CanonicalToolResult(
                name=tool_call.name,
                ok=False,
                text="capability is not allowed for this minion run",
                structured={"reason": "capability_not_allowed", "capability": target_name},
                call_id=tool_call.call_id,
                llm_text="capability is not allowed for this minion run",
                status=RuntimeStatus.ERROR,
            )
        if await self._requires_approval(target_name, tool_call):
            decision = await self._request_approval(target_name, tool_call)
            if decision != "accept":
                self.blocked_summary = f"approval {decision or 'timeout'} for {target_name}"
                return CanonicalToolResult(
                    name=tool_call.name,
                    ok=False,
                    text=f"approval {decision or 'timeout'}",
                    structured={"reason": "approval_not_accepted", "decision": decision or "timeout", "capability": target_name},
                    call_id=tool_call.call_id,
                    llm_text=f"approval {decision or 'timeout'}",
                    status=RuntimeStatus.ERROR,
                )
        return await execution_runtime.execute_tool_async(tool_call, allow_tools=True, turn_id=self.run_id)

    async def _await_with_progress_heartbeat(self, awaitable, *, phase: str, **payload: Any):
        interval = self._heartbeat_interval_seconds()
        if interval <= 0:
            return await awaitable
        task = asyncio.create_task(awaitable)
        heartbeat_count = 0
        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    return await task
                heartbeat_count += 1
                await self._emit_progress(phase, heartbeat_count=heartbeat_count, **payload)
        except BaseException:
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise

    def _heartbeat_interval_seconds(self) -> float:
        metadata = self.pack.metadata if isinstance(self.pack.metadata, dict) else {}
        raw = metadata.get("heartbeat_interval_seconds")
        if raw is None:
            return 30.0
        try:
            interval = float(raw)
        except (TypeError, ValueError):
            return 30.0
        if interval <= 0:
            return 0.0
        return max(0.01, interval)

    async def _requires_approval(self, capability_name: str, tool_call: CanonicalToolCall) -> bool:
        _ = tool_call
        high_risk = {str(item) for item in list((self.pack.approval_policy or {}).get("high_risk_capabilities") or [])}
        return str(capability_name) in high_risk

    async def _request_approval(self, capability_name: str, tool_call: CanonicalToolCall) -> str:
        approval_id = f"appr_{uuid4().hex[:16]}"
        await self._emit(
            "approval_requested",
            {
                "approval_id": approval_id,
                "title": "Minion high-risk operation",
                "requested_action": capability_name,
                "risk": "high",
                "impact": "Minion requested permission before running a high-risk operation.",
                "target": capability_name,
                "args_summary": dict(tool_call.args),
            },
        )
        timeout = float((self.pack.approval_policy or {}).get("decision_timeout_seconds") or 300)
        decision_payload = await self.read_decision(timeout)
        decision = str(((decision_payload or {}).get("decision") or {}).get("decision") or "").strip().lower()
        await self._emit("decision_received", {"approval_id": approval_id, "decision": decision or "timeout"})
        return decision

    async def _commit_current_milestone(self) -> dict[str, Any]:
        repo_path = str((self.pack.workspace or {}).get("repo_path") or "").strip()
        if not repo_path:
            return {"status": "error", "error": "workspace.repo_path is missing"}
        return commit_milestone(
            Path(repo_path),
            work_order_id=self.pack.work_order_id,
            milestone_index=self._current_milestone_index(),
            title=self._current_milestone_title(),
        )

    async def _complete_current_milestone(self, final_text: str) -> dict[str, Any]:
        completion_policy = self._completion_policy()
        evidence = str(completion_policy.get("evidence") or "text_deliverable").strip().lower()
        base_payload = {
            "milestone_index": self._current_milestone_index(),
            "milestone_id": str(self._current_milestone().get("milestone_id") or ""),
            "summary": self._short_summary(final_text or "minion completed current milestone"),
        }
        if evidence == "git_commit":
            await self._persist_text_deliverable_if_needed(final_text)
            checkpoint = await self._commit_current_milestone()
            if checkpoint.get("status") != "committed":
                if checkpoint.get("status") == "no_changes" and self._completion_evidence_present():
                    return {
                        **base_payload,
                        "status": "completed",
                        "commit_sha": str(checkpoint.get("commit_sha") or ""),
                        "git_commit": checkpoint,
                        "evidence": "git_commit",
                        **self._artifact_payload(),
                    }
                blocked_summary = str(
                    checkpoint.get("error")
                    or ("milestone produced no git changes" if checkpoint.get("status") == "no_changes" else "")
                    or f"milestone commit did not complete: {checkpoint.get('status')}"
                )
                return {
                    **base_payload,
                    "status": "blocked",
                    "summary": blocked_summary,
                    "git_commit": checkpoint,
                    **self._artifact_payload(),
                }
            return {
                **base_payload,
                "status": "completed",
                "commit_sha": str(checkpoint.get("commit_sha") or ""),
                "git_commit": checkpoint,
                "evidence": "git_commit",
                **self._artifact_payload(),
            }
        await self._persist_text_deliverable_if_needed(final_text)
        if not str(final_text or "").strip() and not self.produced_artifacts:
            return {
                **base_payload,
                "status": "blocked",
                "summary": "milestone produced no text deliverable",
                "evidence": evidence or "text_deliverable",
                **self._artifact_payload(),
            }
        return {**base_payload, "status": "completed", "evidence": evidence or "text_deliverable", **self._artifact_payload()}

    def _completion_evidence_present(self) -> bool:
        completion_policy = self._completion_policy()
        if str(completion_policy.get("evidence") or "").strip().lower() != "git_commit":
            return False
        repo_path = str((self.pack.workspace or {}).get("repo_path") or "").strip()
        if not repo_path:
            return False
        repo = Path(repo_path)
        if not (repo / ".git").exists():
            return False
        if _repo_has_changes(repo):
            return True
        base_sha = str((self.pack.workspace or {}).get("base_sha") or "").strip()
        head = _repo_head(repo)
        return bool(base_sha and head and head != base_sha)

    async def _persist_text_deliverable_if_needed(self, final_text: str) -> None:
        text = str(final_text or "").strip()
        if not text or self.produced_artifacts:
            return
        if not str((self.pack.workspace or {}).get("artifact_dir") or "").strip():
            return
        artifact = _write_minion_artifact(
            self.pack.workspace,
            {
                "relative_path": f"milestone_{self._current_milestone_index()}_{self._safe_path_part(self.pack.minion_profile)}.md",
                "title": self._current_milestone_title(),
                "role": "primary",
                "mime_type": "text/markdown",
                "content": "\n".join(
                    [
                        f"# {self._current_milestone_title()}",
                        "",
                        f"- work_order_id: {self.pack.work_order_id}",
                        f"- minion_id: {self.minion_id}",
                        f"- run_id: {self.run_id}",
                        "",
                        text,
                        "",
                    ]
                ),
            },
        )
        self._record_produced_artifact(artifact)

    @staticmethod
    def _safe_path_part(value: str) -> str:
        normalized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "").strip())
        return normalized.strip("_")[:80] or "minion"

    async def _emit(self, event_kind: str, payload: dict[str, Any]) -> None:
        event = {
            "type": "event",
            "event_kind": event_kind,
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "work_order_id": self.pack.work_order_id,
            "minion_profile": self.pack.minion_profile,
            "payload": dict(payload),
            "created_at": utc_now(),
        }
        self._append_debug_log("runner_event", event)
        await self.write_event(event)

    async def _emit_progress(self, phase: str, **payload: Any) -> None:
        await self._emit(
            "progress",
            {
                "phase": phase,
                "summary": _progress_summary(phase, payload),
                "milestone_index": self._current_milestone_index(),
                "milestone_title": self._current_milestone_title(),
                **payload,
            },
        )

    def _initial_messages(self) -> list[dict[str, Any]]:
        scaffold = self._prompt_scaffold()
        return [
            {"role": "system", "content": _render_system_prompt(scaffold)},
            {"role": "user", "content": _render_task_prompt(self.pack)},
        ]

    def _prompt_scaffold(self) -> dict[str, Any]:
        profile = dict(self.pack.resolved_profile or {})
        return {
            "identity": str(profile.get("identity_fragment") or ""),
            "behavior": str(profile.get("behavior_fragment") or ""),
            "instruction": self.pack.instruction or self.pack.goal,
            "acceptance_criteria": list(self.pack.acceptance_criteria),
            "continuity": dict(self.pack.continuity),
            "current_milestone": self._current_milestone(),
            "allowed_capabilities": list(self.pack.allowed_capabilities),
            "output_contract": str(profile.get("output_contract_fragment") or ""),
            "workspace_policy": self._workspace_policy(),
            "completion_policy": self._completion_policy(),
        }

    def _workspace_policy(self) -> dict[str, Any]:
        workspace_policy = self.pack.workspace.get("workspace_policy")
        if isinstance(workspace_policy, dict):
            return dict(workspace_policy)
        profile = dict(self.pack.resolved_profile or {})
        if isinstance(profile.get("effective_workspace_policy"), dict):
            return dict(profile.get("effective_workspace_policy") or {})
        return {}

    def _completion_policy(self) -> dict[str, Any]:
        completion_policy = self.pack.workspace.get("completion_policy")
        if isinstance(completion_policy, dict):
            return dict(completion_policy)
        profile = dict(self.pack.resolved_profile or {})
        if isinstance(profile.get("effective_completion_policy"), dict):
            return dict(profile.get("effective_completion_policy") or {})
        return {}

    def _current_milestone(self) -> dict[str, Any]:
        return dict((self.pack.continuity or {}).get("current_milestone") or {})

    def _current_milestone_index(self) -> int:
        try:
            return int(self._current_milestone().get("milestone_index") or 0)
        except (TypeError, ValueError):
            return 0

    def _current_milestone_title(self) -> str:
        return str(self._current_milestone().get("title") or self.pack.instruction or self.pack.goal or "Complete milestone")

    def _terminal_payload(self, status: str, summary: Any) -> dict[str, Any]:
        summary_text = str(summary or "").strip()
        lesson_payload = _extract_lessons_and_clean_summary(summary_text)
        summary_text = self._short_summary(str(lesson_payload.get("summary") or summary_text).strip())
        return {
            "status": str(status or "").strip() or "completed",
            "summary": summary_text,
            "task_lessons": list(lesson_payload.get("task_lessons") or []),
            "system_lessons": list(lesson_payload.get("system_lessons") or []),
            **self._artifact_payload(),
        }

    def _artifact_payload(self) -> dict[str, Any]:
        artifacts = [dict(item) for item in self.produced_artifacts]
        payload: dict[str, Any] = {"artifacts": artifacts}
        primary = next((item for item in artifacts if str(item.get("role") or "") == "primary"), None)
        if primary is None and artifacts:
            primary = artifacts[0]
        if primary is not None:
            payload["primary_artifact"] = dict(primary)
        return payload

    def _record_produced_artifact(self, artifact: dict[str, Any]) -> None:
        path = str(artifact.get("path") or "").strip()
        relative_path = str(artifact.get("relative_path") or "").strip()
        if not path and not relative_path:
            return
        for existing in self.produced_artifacts:
            if path and str(existing.get("path") or "") == path:
                return
            if relative_path and str(existing.get("relative_path") or "") == relative_path:
                return
        self.produced_artifacts.append(dict(artifact))

    @staticmethod
    def _short_summary(value: Any, *, limit: int = 500) -> str:
        text = _compact_preview_text(str(value or ""))
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    def _append_debug_log(self, section: str, payload: dict[str, Any]) -> None:
        config = dict((self.pack.metadata or {}).get("debug_log") or {})
        if not bool(config.get("enabled")):
            return
        path_text = str(config.get("path") or "").strip()
        if not path_text:
            return
        record = {
            "created_at": utc_now(),
            "section": str(section or "debug"),
            "work_order_id": self.pack.work_order_id,
            "minion_profile": self.pack.minion_profile,
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "payload": dict(payload or {}),
        }
        try:
            path = Path(path_text)
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        except Exception:
            return

    @staticmethod
    def _ensure_tool_call_identity(tool_call: CanonicalToolCall) -> CanonicalToolCall:
        call_id = str(getattr(tool_call, "call_id", "") or "").strip() or f"call_{uuid4().hex[:12]}"
        return CanonicalToolCall(name=tool_call.name, args=dict(tool_call.args), call_id=call_id)


def build_slim_minion_runtime(runtime_root: Path) -> MinionRuntimeBundle:
    from pal.core import register_with_core as register_core_with_core

    database = PalV2Database(db_path=Path(runtime_root) / "pal.sqlite3")
    database.initialize(ALL_MODELS)
    llm_repository = LLMEndpointRepository()
    web_search_repository = WebSearchProviderRepository()
    web_fetch_repository = WebFetchProviderRepository()
    if not llm_repository.list_enabled():
        llm_repository.ensure_defaults(DEFAULT_LLM_ENDPOINTS)
    if not web_search_repository.list_all():
        web_search_repository.ensure_defaults(DEFAULT_WEB_SEARCH_PROVIDERS)
    if not web_fetch_repository.list_all():
        web_fetch_repository.ensure_defaults(DEFAULT_WEB_FETCH_PROVIDERS)
    settings = RuntimeSettingRepository()
    settings.ensure_defaults()
    if settings.get("active_web_search_provider_id") is None:
        enabled = web_search_repository.list_enabled()
        if enabled:
            settings.set("active_web_search_provider_id", enabled[0].provider_id)
    if settings.get("active_web_fetch_provider_id") is None:
        enabled = web_fetch_repository.list_enabled()
        if enabled:
            settings.set("active_web_fetch_provider_id", enabled[0].provider_id)

    config = RuntimeConfig.load(Path(runtime_root))
    core = PalCore(config=config)
    core.context.execution_runtime.runtime_root = Path(runtime_root)
    artifact_service = ArtifactManager(runtime_root=Path(runtime_root), repository=ArtifactRepository())
    llm_runtime = LLMRuntime(
        endpoint_resolver=EndpointResolver(repository=llm_repository),
        settings_repository=settings,
        endpoint_invoker=build_default_endpoint_invoker(
            credentials=LiteLLMCredentialResolver(secret_store=EncryptedFileSecretStore(secrets_path=str(Path(runtime_root) / "secrets.json"))),
            artifact_manager=artifact_service,
            runtime_root=runtime_root,
        ),
        config=config,
    )
    register_core_with_core(core)
    register_execution_with_core(core.context)
    register_artifact_with_core(core.context, artifact_service)
    memory_service = MemoryService(
        l3_selector=L3ProviderSelector(
            resolver=core.context.execution_runtime.l3_plugin_registry.require,
            active_provider_id="sqlite_vec_l3",
        )
    )
    register_memory_with_core(core.context, memory_service, config=config)
    l3_plugin = SQLiteVecL3Plugin(service=memory_service)
    memory_service.l3_selector.active_provider_id = l3_plugin.provider_id
    register_l3_with_core(core.context, l3_plugin)
    register_web_search_with_core(core.context, WebSearchService(repository=web_search_repository, settings_repository=settings))
    register_web_fetch_with_core(
        core.context,
        WebFetchService(
            repository=web_fetch_repository,
            settings_repository=settings,
            browser_manager=BrowserServiceManager(runtime_root=Path(runtime_root)),
        ),
    )
    for module_id in ("core", "execution", "artifact", "memory", l3_plugin.module_id, "web_search", "web_fetch"):
        core.publish_module_capabilities(module_id)

    async def close() -> None:
        for handle in tuple(core.context.module_registry.modules.values()):
            shutdown_async = getattr(handle, "shutdown_async", None)
            shutdown_sync = getattr(handle, "shutdown_sync", None)
            if callable(shutdown_async):
                await shutdown_async()
            elif callable(shutdown_sync):
                shutdown_sync()
        database.close()

    return MinionRuntimeBundle(llm_runtime=llm_runtime, execution_runtime=core.context.execution_runtime, close_async=close)


def _llm_tools_for_allowed(execution_runtime: Any, allowed_capabilities: list[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    filtered = filter_minion_allowed_capabilities(allowed_capabilities)
    tool_surface = _minion_llm_tool_surface(filtered)
    for name in tool_surface:
        canonical = str(name or "").strip()
        if not canonical or canonical in seen:
            continue
        spec = execution_runtime.get_capability_spec(canonical)
        if spec is None:
            continue
        seen.add(canonical)
        result.append(
            {
                "type": "function",
                "function": {
                    "name": str(spec.get("name") or canonical),
                    "description": str(spec.get("description") or spec.get("display_name") or canonical),
                    "parameters": dict(spec.get("parameters_schema") or {"type": "object", "properties": {}}),
                },
            }
        )
    return result


MINION_DISCOVERY_TOOL_SURFACE = (
    "op_exec_disc_search",
    "op_exec_disc_read",
    "op_exec_capability_call",
)


MINION_DIRECT_WORK_TOOL_SURFACE = (
    "op_exec_run",
    "op_workspace_tree",
    "op_workspace_search",
    "op_workspace_read",
    "op_minion_artifact_write",
    "op_web_search_query",
    "op_web_fetch_read",
    "op_l3_recall_query",
)


WORKSPACE_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_workspace_tree": {
        "name": "op_workspace_tree",
        "description": "List files under the minion workspace repo_path without modifying anything.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative directory path."},
                "max_depth": {"type": "integer", "default": 2},
                "limit": {"type": "integer", "default": 200},
            },
        },
    },
    "op_workspace_search": {
        "name": "op_workspace_search",
        "description": "Search text files under the minion workspace repo_path without modifying anything.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "description": "Workspace-relative directory path."},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["query"],
        },
    },
    "op_workspace_read": {
        "name": "op_workspace_read",
        "description": "Read a workspace-relative text file without modifying anything.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "default": 1},
                "limit_lines": {"type": "integer", "default": 200},
            },
            "required": ["path"],
        },
    },
    "op_minion_artifact_write": {
        "name": "op_minion_artifact_write",
        "description": "Write a minion deliverable file under workspace.artifact_dir and register it as produced artifact evidence.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string", "description": "Artifact-dir-relative file path, for example plan.md."},
                "content": {"type": "string", "description": "UTF-8 text content to write."},
                "title": {"type": "string"},
                "role": {"type": "string", "description": "Artifact role such as primary, evidence, notes, or tests."},
                "mime_type": {"type": "string", "default": "text/markdown"},
            },
            "required": ["relative_path", "content"],
        },
    },
}


def _minion_llm_tool_surface(allowed_capabilities: list[str]) -> list[str]:
    surface = [
        name
        for name in (*MINION_DISCOVERY_TOOL_SURFACE, *MINION_DIRECT_WORK_TOOL_SURFACE)
        if name in allowed_capabilities
    ]
    if surface:
        return surface
    return allowed_capabilities


@dataclass
class MinionScopedExecutionRuntime:
    base_runtime: Any
    allowed_capabilities: list[str]
    workspace: dict[str, Any] = field(default_factory=dict)
    produced_artifacts: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.allowed_capabilities = filter_minion_allowed_capabilities(self.allowed_capabilities)

    def list_capability_specs(self) -> list[dict[str, Any]]:
        allowed = set(self.allowed_capabilities)
        specs = []
        for name, spec in WORKSPACE_TOOL_SPECS.items():
            if name in allowed and not is_minion_capability_denied(name):
                specs.append(dict(spec))
        for spec in list(self.base_runtime.list_capability_specs()):
            name = str(spec.get("name") or "").strip()
            if name in allowed and not is_minion_capability_denied(name):
                specs.append(spec)
        return specs

    def get_capability_spec(self, name: str) -> dict[str, Any] | None:
        if name in WORKSPACE_TOOL_SPECS:
            if name not in set(self.allowed_capabilities) or is_minion_capability_denied(name):
                return None
            return dict(WORKSPACE_TOOL_SPECS[name])
        spec = self.base_runtime.get_capability_spec(name)
        if spec is None:
            return None
        canonical = str(spec.get("name") or spec.get("canonical_path") or name).strip()
        if canonical not in set(self.allowed_capabilities) or is_minion_capability_denied(canonical):
            return None
        return spec

    async def execute_tool_async(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
        if call.name == "op_exec_disc_search":
            return _capability_result_to_tool_result(
                call,
                ToolSearchTool(runtime=self).invoke(dict(call.args)),
            )
        if call.name == "op_exec_disc_read":
            return _capability_result_to_tool_result(
                call,
                ToolReadTool(runtime=self).invoke(dict(call.args)),
            )
        if call.name in WORKSPACE_TOOL_SPECS:
            result = _workspace_tool_result(call, self.workspace)
            if call.name == "op_minion_artifact_write" and result.ok:
                artifact = dict((result.structured or {}).get("artifact") or result.structured or {})
                if artifact:
                    self.produced_artifacts.append(artifact)
            return result
        return await self.base_runtime.execute_tool_async(call, allow_tools=allow_tools, turn_id=turn_id)


def _capability_result_to_tool_result(call: CanonicalToolCall, result: CapabilityResult) -> CanonicalToolResult:
    return CanonicalToolResult(
        name=call.name,
        ok=result.status == RuntimeStatus.OK,
        text=result.text,
        structured=result.structured,
        call_id=call.call_id,
        llm_text=getattr(result, "llm_text", ""),
        status=result.status,
    )


def _workspace_tool_result(call: CanonicalToolCall, workspace: dict[str, Any]) -> CanonicalToolResult:
    try:
        if call.name == "op_minion_artifact_write":
            artifact = _write_minion_artifact(workspace, call.args)
            payload = {"artifact": artifact}
            text = f"Artifact written: {artifact['relative_path']}"
        else:
            root = _workspace_root(workspace)
            if call.name == "op_workspace_tree":
                payload = _workspace_tree(root, call.args)
                text = "\n".join(item["path"] for item in payload["items"])
            elif call.name == "op_workspace_search":
                payload = _workspace_search(root, call.args)
                text = "\n".join(f"{item['path']}:{item['line_number']}: {item['line']}" for item in payload["matches"])
            elif call.name == "op_workspace_read":
                payload = _workspace_read(root, call.args)
                text = payload["text"]
            else:
                raise ValueError(f"unknown workspace tool: {call.name}")
        return CanonicalToolResult(
            name=call.name,
            ok=True,
            text=text,
            structured=payload,
            call_id=call.call_id,
            llm_text=text,
            status=RuntimeStatus.OK,
        )
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        return CanonicalToolResult(
            name=call.name,
            ok=False,
            text=message,
            structured={"error": message, "error_type": exc.__class__.__name__},
            call_id=call.call_id,
            llm_text=message,
            status=RuntimeStatus.ERROR,
        )


def _workspace_root(workspace: dict[str, Any]) -> Path:
    repo_path = str((workspace or {}).get("repo_path") or "").strip()
    if not repo_path:
        raise ValueError("workspace.repo_path is not available")
    root = Path(repo_path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"workspace.repo_path is not a directory: {root}")
    return root


def _artifact_root(workspace: dict[str, Any]) -> Path:
    artifact_dir = str((workspace or {}).get("artifact_dir") or "").strip()
    if not artifact_dir:
        raise ValueError("workspace.artifact_dir is not available")
    root = Path(artifact_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _artifact_path(root: Path, raw_path: Any) -> Path:
    relative = str(raw_path or "").strip()
    if not relative:
        raise ValueError("relative_path is required")
    if Path(relative).is_absolute():
        raise ValueError("artifact path must be relative to artifact_dir")
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("artifact path escapes artifact_dir")
    if candidate == root:
        raise ValueError("artifact path must name a file")
    return candidate


def _write_minion_artifact(workspace: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    root = _artifact_root(workspace)
    path = _artifact_path(root, args.get("relative_path"))
    content = str(args.get("content") or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    relative_path = str(path.relative_to(root)).replace("\\", "/")
    mime_type = str(args.get("mime_type") or mimetypes.guess_type(path.name)[0] or "text/plain").strip()
    role = str(args.get("role") or "primary").strip() or "primary"
    return {
        "kind": "file",
        "path": str(path),
        "relative_path": relative_path,
        "title": str(args.get("title") or path.stem).strip() or path.name,
        "role": role,
        "mime_type": mime_type,
        "size_bytes": path.stat().st_size,
        "sha256": digest,
    }


def _workspace_path(root: Path, raw_path: Any = "") -> Path:
    relative = str(raw_path or ".").strip() or "."
    candidate = (root / relative).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("workspace path escapes repo_path")
    return candidate


def _workspace_tree(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    base = _workspace_path(root, args.get("path") or ".")
    if not base.exists():
        raise ValueError(f"workspace path does not exist: {base.relative_to(root)}")
    max_depth = max(0, min(_optional_positive_int(args.get("max_depth")) or 2, 8))
    limit = max(1, min(_optional_positive_int(args.get("limit")) or 200, 1000))
    items: list[dict[str, Any]] = []
    if base.is_file():
        stat = base.stat()
        items.append({"path": str(base.relative_to(root)).replace("\\", "/"), "kind": "file", "size_bytes": stat.st_size})
        return {"root": str(root), "items": items, "count": len(items)}
    base_depth = len(base.relative_to(root).parts) if base != root else 0
    for current, dirs, files in os.walk(base):
        current_path = Path(current)
        rel_parts = current_path.relative_to(root).parts if current_path != root else ()
        depth = len(rel_parts) - base_depth
        dirs[:] = [name for name in sorted(dirs) if name not in {".git", "__pycache__", ".pytest_cache"}]
        for name in dirs:
            if len(items) >= limit:
                return {"root": str(root), "items": items, "count": len(items), "truncated": True}
            path = current_path / name
            items.append({"path": str(path.relative_to(root)).replace("\\", "/"), "kind": "dir"})
        for name in sorted(files):
            if len(items) >= limit:
                return {"root": str(root), "items": items, "count": len(items), "truncated": True}
            path = current_path / name
            with contextlib.suppress(OSError):
                items.append({"path": str(path.relative_to(root)).replace("\\", "/"), "kind": "file", "size_bytes": path.stat().st_size})
        if depth >= max_depth:
            dirs[:] = []
    return {"root": str(root), "items": items, "count": len(items)}


def _workspace_search(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    base = _workspace_path(root, args.get("path") or ".")
    limit = max(1, min(_optional_positive_int(args.get("limit")) or 50, 500))
    matches: list[dict[str, Any]] = []
    query_lower = query.lower()
    paths = [base] if base.is_file() else [path for path in base.rglob("*") if path.is_file()]
    for path in paths:
        if ".git" in path.relative_to(root).parts:
            continue
        try:
            for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
                if query_lower not in line.lower():
                    continue
                matches.append(
                    {
                        "path": str(path.relative_to(root)).replace("\\", "/"),
                        "line_number": line_number,
                        "line": _preview_text(line, limit=300),
                    }
                )
                if len(matches) >= limit:
                    return {"root": str(root), "query": query, "matches": matches, "count": len(matches), "truncated": True}
        except OSError:
            continue
    return {"root": str(root), "query": query, "matches": matches, "count": len(matches)}


def _workspace_read(root: Path, args: dict[str, Any]) -> dict[str, Any]:
    path = _workspace_path(root, args.get("path") or "")
    if not path.is_file():
        raise ValueError(f"workspace path is not a file: {path.relative_to(root)}")
    start_line = max(1, _optional_positive_int(args.get("start_line")) or 1)
    limit_lines = max(1, min(_optional_positive_int(args.get("limit_lines")) or 200, 1000))
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    selected = lines[start_line - 1 : start_line - 1 + limit_lines]
    numbered = [f"{index}: {line}" for index, line in enumerate(selected, start=start_line)]
    return {
        "root": str(root),
        "path": str(path.relative_to(root)).replace("\\", "/"),
        "start_line": start_line,
        "line_count": len(selected),
        "truncated": start_line - 1 + limit_lines < len(lines),
        "text": "\n".join(numbered),
    }


def _effective_capability_name(tool_call: CanonicalToolCall) -> str:
    if tool_call.name == "op_exec_capability_call":
        return str(tool_call.args.get("name") or tool_call.name).strip()
    return tool_call.name


def _assistant_tool_message(text: str, tool_calls: list[CanonicalToolCall]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": text or "",
        "tool_calls": [
            {
                "id": str(tool_call.call_id or ""),
                "type": "function",
                "function": {"name": tool_call.name, "arguments": json.dumps(tool_call.args, ensure_ascii=False, sort_keys=True)},
            }
            for tool_call in tool_calls
        ],
    }


def _tool_result_text(result: CanonicalToolResult) -> str:
    if str(result.llm_text or "").strip():
        return str(result.llm_text).strip()
    if str(result.text or "").strip():
        return str(result.text).strip()
    if result.structured:
        return json.dumps(result.structured, ensure_ascii=False, sort_keys=True)
    return "tool completed" if result.ok else "tool failed"


def _tool_call_summary(tool_call: CanonicalToolCall) -> dict[str, str]:
    return {
        "tool_name": str(tool_call.name or ""),
        "target_name": _effective_capability_name(tool_call),
        "call_id": str(tool_call.call_id or ""),
    }


def _progress_summary(phase: str, payload: dict[str, Any]) -> str:
    phase_name = str(phase or "progress").strip() or "progress"
    if phase_name == "llm_round_started":
        return f"LLM round {payload.get('round')} started"
    if phase_name == "llm_round_completed":
        calls = list(payload.get("tool_calls") or [])
        if calls:
            names = ", ".join(str(item.get("target_name") or item.get("tool_name") or "") for item in calls[:4] if isinstance(item, dict))
            extra = "..." if len(calls) > 4 else ""
            return f"LLM round {payload.get('round')} requested tools: {names}{extra}".strip()
        return f"LLM round {payload.get('round')} produced final text"
    if phase_name == "tool_call_started":
        return f"Tool started: {payload.get('target_name') or payload.get('tool_name')}"
    if phase_name == "tool_call_completed":
        status = "ok" if bool(payload.get("ok")) else "error"
        return f"Tool completed: {payload.get('target_name') or payload.get('tool_name')} ({status})"
    if phase_name == "tool_call_failed":
        return f"Tool failed: {payload.get('target_name') or payload.get('tool_name')}"
    if phase_name == "milestone_finalizing":
        return "Milestone finalizing"
    return phase_name.replace("_", " ")


def _json_preview(value: Any, *, limit: int = 500) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = str(value)
    return _preview_text(text, limit=limit)


def _preview_text(value: Any, *, limit: int = 400) -> str:
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _resolve_minion_max_output_tokens(llm_runtime: Any, pack: TaskContextPack) -> int:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    explicit = _optional_positive_int(metadata.get("max_output_tokens"))
    if explicit is not None:
        return explicit
    preferred_endpoint_id = _preferred_endpoint_id_from_pack(pack)
    resolved = _runtime_max_output_tokens(llm_runtime, preferred_endpoint_id=preferred_endpoint_id)
    if resolved is not None:
        return resolved
    facts = _runtime_endpoint_facts(llm_runtime, preferred_endpoint_id=preferred_endpoint_id)
    fact_max = _optional_positive_int(facts.get("max_output_tokens")) if facts else None
    if fact_max is not None:
        return fact_max
    context_window = _optional_positive_int(facts.get("context_window")) if facts else None
    if context_window is not None:
        return _max_output_tokens_from_context_window(context_window, llm_runtime)
    config = getattr(llm_runtime, "config", None)
    return _optional_positive_int(getattr(config, "fallback_max_output_tokens", None)) or 4096


def _minion_llm_request_metadata(pack: TaskContextPack, run_id: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "response_mode_hint": "operational",
        "minion_run_id": str(run_id or ""),
        "max_output_tokens_source": "minion",
    }
    preferred_endpoint_id = _preferred_endpoint_id_from_pack(pack)
    if preferred_endpoint_id:
        metadata["preferred_endpoint_id"] = preferred_endpoint_id
    return metadata


def _preferred_endpoint_id_from_pack(pack: TaskContextPack) -> str | None:
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    value = str(metadata.get("preferred_endpoint_id") or "").strip()
    return value or None


def _runtime_max_output_tokens(llm_runtime: Any, *, preferred_endpoint_id: str | None = None) -> int | None:
    resolver = getattr(llm_runtime, "resolve_max_output_tokens", None)
    if not callable(resolver):
        return None
    with contextlib.suppress(Exception):
        try:
            return _optional_positive_int(resolver(preferred_endpoint_id=preferred_endpoint_id))
        except TypeError:
            return _optional_positive_int(resolver())
    return None


def _runtime_endpoint_facts(llm_runtime: Any, *, preferred_endpoint_id: str | None = None) -> dict[str, Any]:
    resolver = getattr(llm_runtime, "resolve_endpoint_facts", None)
    if not callable(resolver):
        return {}
    with contextlib.suppress(Exception):
        try:
            facts = resolver(preferred_endpoint_id=preferred_endpoint_id)
        except TypeError:
            facts = resolver()
        return dict(facts) if isinstance(facts, dict) else {}
    return {}


def _max_output_tokens_from_context_window(context_window: int, llm_runtime: Any) -> int:
    config = getattr(llm_runtime, "config", None)
    cap = _optional_positive_int(getattr(config, "default_max_output_tokens", None)) or 25_000
    floor = _optional_positive_int(getattr(config, "fallback_max_output_tokens", None)) or 4096
    margin_factor = float(getattr(config, "context_margin_factor", 0.05) or 0.05)
    margin_cap = _optional_positive_int(getattr(config, "context_margin_cap", None)) or 16_384
    margin_min = _optional_positive_int(getattr(config, "context_margin_min", None)) or 1024
    margin = min(margin_cap, max(margin_min, int(context_window * margin_factor)))
    usable = max(512, context_window - margin)
    context_fraction = max(512, context_window // 4)
    return max(512, min(cap, max(floor, context_fraction), usable))


def _repo_has_changes(repo_path: Path) -> bool:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return True
    return bool((completed.stdout or "").strip())


def _repo_head(repo_path: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return ""
    if completed.returncode != 0:
        return ""
    return str(completed.stdout or "").strip()


def _render_system_prompt(scaffold: dict[str, Any]) -> str:
    completion_policy = scaffold.get("completion_policy") or {}
    testing_guidance = ""
    if isinstance(completion_policy, dict) and bool(completion_policy.get("requires_developer_tests")):
        testing_guidance = (
            "Completion requires developer test evidence: before completing, state the focused test plan, "
            "run the relevant tests/checks available through listed capabilities, fix failures you caused, "
            "and report blocked instead of completed if tests cannot be run or cannot pass with concrete evidence.\n"
        )
    return (
        f"{scaffold.get('identity')}\n\n"
        f"{scaffold.get('behavior')}\n\n"
        "Your context is the task context pack, the current milestone, and the listed capabilities.\n"
        "Use only the listed capabilities. Report by milestone, never by percentage or ETA.\n"
        "Use `op_l3_recall_query` when prior Pal experience, project lessons, or user preferences may materially improve the result.\n"
        "If capability evidence is required, use a relevant listed capability before completing the milestone.\n"
        f"{testing_guidance}"
        "If completion evidence cannot be produced, report blocked instead of completed.\n"
        "When completion policy requires git_commit, leave file changes in the workspace; do not run git commit yourself.\n"
        "When `op_minion_artifact_write` is available, write your primary deliverable to workspace.artifact_dir with that tool; keep the final chat summary short and point to the artifact.\n"
        "If a tool/capability call fails and `op_l3_recall_query` is listed below, you MUST call `op_l3_recall_query` for relevant prior experience before retrying, debugging further, or reporting blocked.\n"
        "When the current milestone is complete, stop with a concise milestone summary. "
        "If the run taught something genuinely reusable, include separate Task lessons or System lessons; Pal will ask the user before absorbing them.\n\n"
        f"Workspace policy:\n{json.dumps(scaffold.get('workspace_policy') or {}, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Completion policy:\n{json.dumps(scaffold.get('completion_policy') or {}, ensure_ascii=False, sort_keys=True)}\n\n"
        f"Output contract:\n{scaffold.get('output_contract')}\n\n"
        f"Allowed capabilities:\n{json.dumps(scaffold.get('allowed_capabilities') or [], ensure_ascii=False)}"
    ).strip()


def _render_task_prompt(pack: TaskContextPack) -> str:
    payload = {
        "work_order_id": pack.work_order_id,
        "goal": pack.goal,
        "instruction": pack.instruction,
        "acceptance_criteria": list(pack.acceptance_criteria),
        "workspace": dict(pack.workspace),
        "continuity": dict(pack.continuity),
        "artifacts": list(pack.artifacts),
        "memory_pack": dict(pack.memory_pack),
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _extract_lessons_and_clean_summary(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    lessons = {"task_lessons": [], "system_lessons": []}
    if not raw:
        return {"summary": "", **lessons}
    loaded = _try_extract_json(raw)
    if isinstance(loaded, dict):
        lessons["task_lessons"].extend(_string_items(loaded.get("task_lessons") or loaded.get("taskLessons") or loaded.get("task_lessons_to_remember")))
        lessons["system_lessons"].extend(_string_items(loaded.get("system_lessons") or loaded.get("systemLessons") or loaded.get("system_lesson_candidates")))
        summary = str(loaded.get("summary") or loaded.get("final_summary") or loaded.get("result") or raw).strip()
        return {"summary": summary, **{key: _dedupe_nonempty(value) for key, value in lessons.items()}}

    current: str | None = None
    summary_lines: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip().strip("-* ")
        heading = _lesson_heading_kind(stripped)
        if heading == "task_lessons":
            current = "task_lessons"
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif heading == "system_lessons":
            current = "system_lessons"
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif current and stripped:
            value = stripped
        else:
            current = None
            value = ""
            summary_lines.append(line.rstrip())
        if current and value and value.lower() not in {"none", "n/a"}:
            lessons[current].append(value)
    summary_text = "\n".join(summary_lines).strip()
    return {"summary": summary_text, **{key: _dedupe_nonempty(value) for key, value in lessons.items()}}


def _compact_preview_text(value: str) -> str:
    lines: list[str] = []
    blank_pending = False
    for raw_line in str(value or "").strip().splitlines():
        line = " ".join(raw_line.strip().split())
        if not line:
            blank_pending = bool(lines)
            continue
        if blank_pending:
            lines.append("")
            blank_pending = False
        lines.append(line)
    return "\n".join(lines).strip()


def _lesson_heading_kind(text: str) -> str:
    normalized = str(text or "").strip().strip("#*_` ")
    while normalized and not (normalized[0].isalnum() or normalized[0] == "_"):
        normalized = normalized[1:].strip()
    lowered = normalized.lower().replace("_", " ")
    lowered = lowered.rstrip(":").strip()
    if lowered in {"task lesson", "task lessons", "task wise lessons", "task-wise lessons"}:
        return "task_lessons"
    if lowered in {"system lesson", "system lessons", "system wise lessons", "system-wise lessons"}:
        return "system_lessons"
    if lowered.startswith(("task lesson:", "task lessons:", "task wise lessons:", "task-wise lessons:")):
        return "task_lessons"
    if lowered.startswith(("system lesson:", "system lessons:", "system wise lessons:", "system-wise lessons:")):
        return "system_lessons"
    return ""


def _extract_lessons(text: str) -> dict[str, list[str]]:
    payload = _extract_lessons_and_clean_summary(text)
    return {
        "task_lessons": list(payload.get("task_lessons") or []),
        "system_lessons": list(payload.get("system_lessons") or []),
    }
    raw = str(text or "").strip()
    lessons = {"task_lessons": [], "system_lessons": []}
    if not raw:
        return lessons
    loaded = _try_extract_json(raw)
    if isinstance(loaded, dict):
        lessons["task_lessons"].extend(_string_items(loaded.get("task_lessons") or loaded.get("taskLessons") or loaded.get("task_lessons_to_remember")))
        lessons["system_lessons"].extend(_string_items(loaded.get("system_lessons") or loaded.get("systemLessons") or loaded.get("system_lesson_candidates")))
    current: str | None = None
    for line in raw.splitlines():
        stripped = line.strip().strip("-* ")
        lowered = stripped.lower()
        if lowered.startswith(("task lesson:", "task lessons:", "task_lessons:", "task-wise lessons:", "task wise lessons:")):
            current = "task_lessons"
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif lowered.startswith(("system lesson:", "system lessons:", "system_lessons:", "system-wise lessons:", "system wise lessons:")):
            current = "system_lessons"
            value = stripped.split(":", 1)[1].strip() if ":" in stripped else ""
        elif current and stripped:
            value = stripped
        else:
            value = ""
        if current and value and value.lower() not in {"none", "n/a", "无", "没有"}:
            lessons[current].append(value)
    return {key: _dedupe_nonempty(value) for key, value in lessons.items()}


def _try_extract_json(text: str) -> Any:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except Exception:
        return None


def _string_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    return []


def _dedupe_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = " ".join(str(value or "").split())
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result

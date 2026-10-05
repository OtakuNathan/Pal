"""Idle-only, user-controlled model selection and optional compaction."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pal.control import interactions
from pal.control.contracts import ControlAction
from pal.core.compaction_coordinator import CompactionTrigger
from pal.core.contracts import PendingControlRequest
from pal.llm.contracts import LLM_RUNTIME
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.conversions import tool_definition_ir_from_dict
from pal.llm.ir import LLMRequestIR, PromptRegionIR, ThinkingLevel
from pal.memory.context_view import projected_messages
from pal.memory.contracts import MEMORY
from pal.shared import PromptAssemblyContext

if TYPE_CHECKING:
    from pal.llm.runtime import LLMRuntime, ModelSwitchAdvice


class ModelSwitchMixin:
    def _model_switch_busy_reason(self) -> str:
        if (self.turn_manager.latest_active_turn_id() is not None
                or any(not task.done() for task in self.state.turn_tasks.values())):
            return "Model switching is unavailable while a turn is active or finishing. Try again after it ends."
        if self.state.pending_channel_turns:
            return "Model switching is unavailable while earlier messages are waiting. Try again after they finish."
        if self.state.resident_quiescing or self.state.memory_maintenance or self._compaction_gate_active():
            return "Model switching is unavailable during compaction or maintenance. Try again later."
        return ""

    def _model_selection_identity(self, llm: LLMRuntime, target_id: str) -> str:
        endpoints = [
            LLMEndpointSpec.from_value(endpoint).to_payload()
            for endpoint in self._llm_model_endpoints(llm)
            if endpoint.endpoint_id in {llm.active_endpoint_id, target_id}
        ]
        return json.dumps([llm.active_endpoint_id, endpoints], sort_keys=True, default=str)

    def _model_switch_prompt(self, endpoint: Any, think_level: str) -> LLMRequestIR:
        memory = self.context.require_port(MEMORY)
        prompt = self.prompt_compiler.build_canonical_prompt(
            PromptAssemblyContext(metadata={"typed_l1_projection": True}),
            model_hint=endpoint.model_id,
            max_output_tokens=endpoint.max_output_tokens or self.config.fallback_max_output_tokens,
        )
        history = memory.project_continuity([
            message for turn in memory.history.turns
            for message in projected_messages(turn, settled=True)
        ])
        return replace(
            prompt,
            messages=(*prompt.messages, *(replace(m, prompt_region=PromptRegionIR.SETTLED_HISTORY) for m in history)),
            tools=tuple(tool_definition_ir_from_dict(tool) for tool in self._build_llm_tool_contracts()),
            policy=replace(prompt.policy, thinking_level=ThinkingLevel(think_level)),
            metadata={**dict(prompt.metadata), "preferred_endpoint_id": endpoint.endpoint_id},
        )

    def _model_switch_advice(self, llm: LLMRuntime, target_id: str, think_level: str) -> ModelSwitchAdvice:
        endpoint = next((ep for ep in self._llm_model_endpoints(llm) if ep.endpoint_id == target_id), None)
        if endpoint is None:
            raise ValueError("Target endpoint is no longer enabled.")
        return llm.model_switch_advice(target_id, self._model_switch_prompt(endpoint, think_level))

    async def _handle_set_model_async(self, action: ControlAction) -> None:
        if action.route is None:
            return
        llm = self.context.require_port(LLM_RUNTIME)
        self._refresh_llm_runtime_settings(llm)
        target_id = str(action.args.get("endpoint_id") or "").strip()
        endpoints = self._llm_model_endpoints(llm)
        if not any(ep.endpoint_id == target_id for ep in endpoints):
            await self._complete_action_reply_async(action, f"Unknown enabled model endpoint: {target_id}.\nAvailable endpoints: {', '.join(ep.endpoint_id for ep in endpoints)}")
            return
        if target_id == llm.active_endpoint_id:
            self._discard_model_selection_draft(action.route.control_scope_key)
            # Explicitly reselecting a subscription endpoint is also its
            # existing usage-pause recovery action.
            from pal.llm.runtime import LLMRuntime
            if isinstance(llm, LLMRuntime):
                llm.resume_subscription(target_id)
            await self._deliver_control_delivery_async(interactions.think_panel_delivery(
                action.route, llm.thinking_status(target_id), back_to_models=True,
            ))
            return
        async with self.state.channel_turn_transition_lock:
            busy = self._model_switch_busy_reason()
        if busy:
            await self._complete_action_reply_async(action, busy)
            return
        status = llm.thinking_status(target_id)
        try:
            advice = self._model_switch_advice(llm, target_id, status["current"])
        except Exception as exc:
            await self._complete_action_reply_async(action, f"Cannot switch models: {exc}")
            return
        request = PendingControlRequest(
            request_id=f"model_{uuid4().hex[:12]}", request_kind="model_switch",
            control_scope_key=action.route.control_scope_key, route=action.route,
            expires_at=(datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
            payload={"target_id": target_id, "identity": self._model_selection_identity(llm, target_id),
                     "stage": "confirm" if advice.compact_required else "think", "compact_confirmed": False},
        )
        scope = self._ensure_scope_state(action.route.control_scope_key)
        previous = scope.pending_requests.get("model_switch")
        if previous is not None and previous.payload.get("stage") == "executing":
            await self._complete_action_reply_async(action, "A model switch is already executing.")
            return
        scope.pending_requests["model_switch"] = request
        await self._render_model_switch_async(request, status)

    async def _render_model_switch_async(
        self, request: PendingControlRequest, status: dict[str, Any] | None = None,
    ) -> None:
        if status is None:
            status = self.context.require_port(LLM_RUNTIME).thinking_status(request.payload["target_id"])
        if request.payload["stage"] == "think" and len(status.get("choices", ())) == 1:
            await self._handle_model_switch_action_async(ControlAction(
                action_kind="model_switch_step", target_scope="runtime", route=request.route,
                args={"request_id": request.request_id, "operation": "think", "think_level": status["choices"][0]["id"]},
            ))
            return
        await self._deliver_control_delivery_async(interactions.model_switch_delivery(request, status))

    async def _handle_model_switch_action_async(self, action: ControlAction) -> None:
        if action.route is None:
            return
        scope = self._ensure_scope_state(action.route.control_scope_key)
        request = scope.pending_requests.get("model_switch")
        if (request is None or request.request_id != action.args.get("request_id")
                or request.route.endpoint_id != action.route.endpoint_id):
            await self._complete_action_reply_async(action, "Model selection is missing, expired, or already consumed. Use /model again.")
            return
        operation = action.args.get("operation")
        if operation == "cancel":
            scope.pending_requests.pop("model_switch", None)
            if request.payload.get("stage") == "executing":
                async with self.state.channel_turn_transition_lock:
                    self._compaction_gate().cancel("pal:resident", reason="model_switch_cancelled")
                    self.context.require_port(MEMORY).cancel_active_compaction(reason="model_switch_cancelled")
            await self._complete_action_reply_async(action, "Model selection cancelled.")
            await self._handle_show_model_async(action)
            return
        if request.payload["stage"] == "executing":
            await self._complete_action_reply_async(action, "This model selection is already executing.")
            return
        llm = self.context.require_port(LLM_RUNTIME)
        self._refresh_llm_runtime_settings(llm)
        target_id = request.payload["target_id"]
        if request.payload["identity"] != self._model_selection_identity(llm, target_id):
            scope.pending_requests.pop("model_switch", None)
            await self._complete_action_reply_async(action, "Model configuration changed. Use /model again.")
            return
        if operation == "confirm" and request.payload["stage"] == "confirm":
            request.payload.update(stage="think", compact_confirmed=True)
            await self._render_model_switch_async(request)
            return
        if operation != "think" or request.payload["stage"] != "think":
            await self._complete_action_reply_async(action, "Complete the current /model selection step first.")
            return
        status = llm.thinking_status(target_id)
        level = str(action.args.get("think_level") or status["current"])
        if level not in {choice["id"] for choice in status["choices"]}:
            await self._complete_action_reply_async(action, "Invalid thinking level for the target model.")
            return
        async with self.state.channel_turn_transition_lock:
            busy = self._model_switch_busy_reason()
            ticket = None if busy else self._compaction_gate().claim(
                "pal:resident", trigger=CompactionTrigger.MODEL_SWITCH,
                deadline_seconds=self.turn_executor.compaction_deadline_seconds(),
            )
            if ticket is not None:
                request.payload["stage"] = "executing"
        if ticket is None:
            scope.pending_requests.pop("model_switch", None)
            await self._complete_action_reply_async(action, busy or "Model switching is busy. Use /model again later.")
            return

        def current() -> bool:
            held = self._compaction_gate().ticket_for(ticket.scope)
            return (scope.pending_requests.get("model_switch") is request
                    and held is not None and held.op_id == ticket.op_id
                    and not held.cancelled and not held.expired
                    and request.payload["identity"] == self._model_selection_identity(llm, target_id)
                    and llm.settings_repository.get_active_llm_endpoint_id() == llm.active_endpoint_id)

        compact_result = None
        try:
            advice = self._model_switch_advice(llm, target_id, level)
            if advice.compact_required and not request.payload["compact_confirmed"]:
                request.payload["stage"] = "confirm"
                await self._render_model_switch_async(request, status)
                return
            await self.cache_warm_deadline.clear_for_user_activity()
            if advice.compact_required:
                source = llm.active_endpoint_id
                if not source:
                    raise ValueError("Select the previous model first to Compact this history, or use /reset.")
                await self._deliver_control_delivery_async(interactions.terminal_delivery_for_action(
                    action, "Compacting on the current model before switching…",
                    delivery_kind="interactive_update",
                ))
                await self._flush_control_status_async(action.route)
                compact_result = await self._run_control_compaction_async(
                    ticket, preferred_endpoint_id=source,
                    target_input_budget=advice.target_input_budget,
                    reserved_output_tokens=advice.reserved_output_tokens,
                    commit_guard=current,
                )
                if not compact_result.success:
                    raise ValueError(f"Compact did not complete ({compact_result.status}); the model was not changed.")
            async with self.state.channel_turn_transition_lock:
                if not current():
                    raise ValueError("Model selection was cancelled or its configuration changed.")
                if self._model_switch_advice(llm, target_id, level).compact_required:
                    raise ValueError("The current context still cannot be used by the target model.")
                llm.apply_model_selection(target_id, level)
                scope.pending_requests.pop("model_switch", None)
        except Exception as exc:
            suffix = " The completed Compact remains in effect." if compact_result and compact_result.success else ""
            await self._complete_action_reply_async(action, f"Model switch failed: {exc}{suffix}")
        else:
            # Delivery failure cannot roll back an already committed selection.
            # Let the channel error propagate without reporting a switch failure.
            await self._complete_action_reply_async(action, f"Model updated to {target_id}. Thinking level: {level}. This applies to new turns only.")
        finally:
            if request.payload.get("stage") == "executing" and scope.pending_requests.get("model_switch") is request:
                scope.pending_requests.pop("model_switch", None)
            try:
                if compact_result and compact_result.success:
                    await self._deliver_compact_candidates_async(action, compact_result.memory_result)
            finally:
                async with self.state.channel_turn_transition_lock:
                    self._compaction_gate().release(ticket)
                await self._start_next_queued_turn_async()

    def _invalidate_model_selections(self) -> None:
        for scope in self.state.control_scopes.values():
            scope.pending_requests.pop("model_switch", None)

    def _discard_model_selection_draft(self, scope_key: str) -> None:
        scope = self._ensure_scope_state(scope_key)
        request = scope.pending_requests.get("model_switch")
        if request is not None and request.payload.get("stage") != "executing":
            scope.pending_requests.pop("model_switch", None)

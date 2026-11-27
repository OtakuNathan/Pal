"""LLM-owned model controls using Core's local-request facilities."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pal.control import interactions
from pal.control.contracts import ControlAction
from pal.control.runtime_contracts import CORE_CONTROL, ControlBusyError, ControlRuntimePort
from pal.core.contracts import PendingControlRequest
from pal.core.control_action_registry import ControlActionSpec
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.ir import LLMRequestIR, ThinkingLevel
from pal.llm import control_presentation as presentation
from pal.foundation.diagnostics import exception_reason

if TYPE_CHECKING:
    from pal.core.main_context import MainContext
    from pal.llm.runtime import LLMRuntime, ModelSwitchAdvice
    from pal.llm.models import LLMEndpointModel


class LLMControl:
    def __init__(self, context: MainContext, runtime: LLMRuntime, status: Callable[[], str]) -> None:
        self.runtime = runtime
        self._core = context.port_registry.observe(CORE_CONTROL)
        self.status = status

    @property
    def _host(self) -> ControlRuntimePort:
        return self._core.current

    def handlers(self) -> dict[str, ControlActionSpec]:
        return {
            "show_think": ControlActionSpec(self.show_think),
            "set_think": ControlActionSpec(self.set_think),
            "show_model": ControlActionSpec(self._handle_show_model_async),
            "set_model": ControlActionSpec(self._handle_set_model_async),
            "model_switch_step": ControlActionSpec(self._handle_model_switch_action_async),
            "refresh_llm_endpoint": ControlActionSpec(self.refresh),
            "show_llm_status": ControlActionSpec(self.show_status, allow_during_maintenance=True),
        }

    async def show_status(self, action: ControlAction) -> None:
        await self._host.reply(action, self.status())

    async def show_think(self, action: ControlAction) -> None:
        self.runtime.refresh_runtime_settings()
        await self._host.deliver(presentation.think_panel_delivery(action.route, self.runtime.thinking_status()))

    async def set_think(self, action: ControlAction) -> None:
        try:
            requested = str(action.args.get("think_level") or "").strip()
            resolved = await self._host.configuration_change(
                lambda: self.runtime.set_think_level(requested), reason="llm_think_change")
        except ValueError as exc:
            await self._host.reply(action, exception_reason(exc))
            return
        status = self.runtime.thinking_status()
        endpoint = status.get("endpoint_id") or "the active endpoint"
        mode = status.get("effort_update_mode")
        if mode == "compact":
            timing = "This takes effect on new turns after the next successful compact."
        elif mode == "append":
            timing = "New turns append the effort change; thinking-mode changes wait for compact."
        else:
            timing = "This applies to new turns only."
        await self._host.reply(action, f"Think level for {endpoint} updated to {resolved}. {timing}")

    async def _handle_show_model_async(self, action: ControlAction) -> None:
        if action.route is not None:
            self._discard_model_selection_draft(action.route.control_scope_key)
        self.runtime.refresh_runtime_settings()
        await self._host.deliver(presentation.model_panel_delivery(
            action.route, self.runtime.endpoint_resolver.enabled(), self.runtime.active_endpoint_id))

    async def refresh(self, action: ControlAction) -> None:
        payload = await self._host.configuration_change(self.runtime.refresh_llm_endpoints,
                                                      reason="llm_endpoint_refresh")
        errors = await self._host.notify("llm.endpoints.changed", payload)
        lines = ["LLM endpoints refreshed.",
                 f"Enabled endpoints: {payload.get('enabled_count', '-')}",
                 f"Primary endpoint for future turns: {payload.get('primary_endpoint_id') or '-'}",
                 f"Active endpoint setting: {payload.get('configured_active_endpoint_id') or '-'}"]
        for key, title in (("added_endpoint_ids", "Added"), ("removed_endpoint_ids", "Removed/disabled")):
            if payload.get(key):
                lines.append(f"{title}: {', '.join(str(value) for value in payload[key])}")
        lines.extend(f"Dependent LLM runtime refresh failed ({owner}): {error}" for owner, error in errors.items())
        await self._host.reply(action, "\n".join(lines))

    def _model_switch_busy_reason(self) -> str:
        reason = self._host.busy_reason()
        return f"Model switching is unavailable while {reason}. Try again later." if reason else ""

    def _model_selection_identity(self, llm: LLMRuntime, target_id: str) -> str:
        endpoints = [
            LLMEndpointSpec.from_value(endpoint).to_payload()
            for endpoint in llm.endpoint_resolver.enabled()
            if endpoint.endpoint_id in {llm.active_endpoint_id, target_id}
        ]
        return json.dumps([llm.active_endpoint_id, endpoints], sort_keys=True, default=str)

    def _model_switch_prompt(self, endpoint: LLMEndpointModel, think_level: str) -> LLMRequestIR:
        prompt = self._host.capture_prompt()
        return replace(prompt, model_hint=endpoint.model_id,
            policy=replace(prompt.policy, thinking_level=ThinkingLevel(think_level),
                           max_output_tokens=endpoint.max_output_tokens or prompt.policy.max_output_tokens),
            metadata={**dict(prompt.metadata), "preferred_endpoint_id": endpoint.endpoint_id})

    def _model_switch_advice(self, llm: LLMRuntime, target_id: str, think_level: str) -> ModelSwitchAdvice:
        endpoint = next((ep for ep in llm.endpoint_resolver.enabled() if ep.endpoint_id == target_id), None)
        if endpoint is None:
            raise ValueError("Target endpoint is no longer enabled.")
        return llm.model_switch_advice(target_id, self._model_switch_prompt(endpoint, think_level))

    async def _handle_set_model_async(self, action: ControlAction) -> None:
        if action.route is None:
            return
        llm = self.runtime
        llm.refresh_runtime_settings()
        target_id = str(action.args.get("endpoint_id") or "").strip()
        endpoints = llm.endpoint_resolver.enabled()
        if not any(ep.endpoint_id == target_id for ep in endpoints):
            await self._host.reply(action, f"Unknown enabled model endpoint: {target_id}.\nAvailable endpoints: {', '.join(ep.endpoint_id for ep in endpoints)}")
            return
        if target_id == llm.active_endpoint_id:
            self._discard_model_selection_draft(action.route.control_scope_key)
            # Explicitly reselecting a subscription endpoint is also its
            # existing usage-pause recovery action.
            llm.resume_subscription(target_id)
            await self._host.deliver(presentation.think_panel_delivery(
                action.route, llm.thinking_status(target_id), back_to_models=True,
            ))
            return
        busy = self._model_switch_busy_reason()
        if busy:
            await self._host.reply(action, busy)
            return
        status = llm.thinking_status(target_id)
        try:
            advice = self._model_switch_advice(llm, target_id, status["current"])
        except Exception as exc:
            await self._host.reply(action, f"Cannot switch models: {exception_reason(exc)}")
            return
        request = PendingControlRequest(
            request_id=f"model_{uuid4().hex[:12]}", request_kind="model_switch",
            control_scope_key=action.route.control_scope_key, route=action.route,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=60),
            payload={"target_id": target_id, "identity": self._model_selection_identity(llm, target_id),
                     "stage": "confirm" if advice.compact_required else "think", "compact_confirmed": False,
                     "interactive": interactions.is_interaction_action(action)},
            context_sensitive=True,
        )
        scope = self._host.requests
        previous = scope.get(action.route.control_scope_key, "model_switch")
        if previous is not None and previous.payload.get("stage") == "executing":
            await self._host.reply(action, "A model switch is already executing.")
            return
        request.expired_delivery = interactions.terminal_delivery_for_interaction(
            request.route, interaction_id=interactions.control_panel_interaction_id(request.route),
            interaction_kind="control_panel", text="This model selection expired.",
            delivery_kind="interactive_expire")
        scope.put(request)
        await self._render_model_switch_async(request, status)

    async def _render_model_switch_async(
        self, request: PendingControlRequest, status: dict[str, Any] | None = None,
    ) -> None:
        if status is None:
            status = self.runtime.thinking_status(request.payload["target_id"])
        if request.payload["stage"] == "think" and len(status.get("choices", ())) == 1:
            args = {"request_id": request.request_id, "operation": "think", "think_level": status["choices"][0]["id"]}
            if request.payload.get("interactive"):
                args.update(interaction_origin="button",
                            interaction_id=interactions.control_panel_interaction_id(request.route),
                            interaction_kind="control_panel")
            await self._handle_model_switch_action_async(ControlAction(
                action_kind="model_switch_step", target_scope="runtime", route=request.route,
                args=args,
            ))
            return
        request.payload["interactive"] = True
        await self._host.deliver(presentation.model_switch_delivery(request, status))

    async def _complete_model_switch_reply_async(
        self, action: ControlAction, request: PendingControlRequest, text: str,
        *, preserve_panel: bool = False,
    ) -> None:
        if preserve_panel:
            action = replace(action, args={**action.args, "interaction_origin": ""})
        elif request.payload.get("interactive") and not interactions.is_interaction_action(action):
            await self._host.deliver(interactions.terminal_delivery_for_interaction(
                request.route,
                interaction_id=interactions.control_panel_interaction_id(request.route),
                interaction_kind="control_panel", text=text,
            ))
        await self._host.reply(action, text)

    async def _handle_model_switch_action_async(self, action: ControlAction) -> None:
        if action.route is None:
            return
        scope = self._host.requests
        request = scope.get(action.route.control_scope_key, "model_switch")
        if (request is None or request.request_id != action.args.get("request_id")
                or request.route.endpoint_id != action.route.endpoint_id):
            # A stale selection must not resolve the shared panel now showing
            # another draft or the model list.
            action = replace(action, args={**action.args, "interaction_origin": ""})
            await self._host.reply(action, "Model selection is missing, expired, or already consumed. Use /model again.")
            return
        if not request.executing and request.expired:
            await scope.expire(request)
            return
        operation = action.args.get("operation")
        if operation == "cancel":
            scope.remove(action.route.control_scope_key, "model_switch")
            if request.payload.get("stage") == "executing":
                await self._host.cancel_context_change(reason="model_switch_cancelled")
            if not interactions.is_interaction_action(action):
                await self._host.reply(action, "Model selection cancelled.")
            await self._handle_show_model_async(action)
            return
        if request.payload["stage"] == "executing":
            await self._complete_model_switch_reply_async(
                action, request, "This model selection is already executing.", preserve_panel=True,
            )
            return
        llm = self.runtime
        llm.refresh_runtime_settings()
        target_id = request.payload["target_id"]
        if request.payload["identity"] != self._model_selection_identity(llm, target_id):
            scope.remove(action.route.control_scope_key, "model_switch")
            await self._complete_model_switch_reply_async(action, request, "Model configuration changed. Use /model again.")
            return
        if operation == "confirm" and request.payload["stage"] == "confirm":
            request.payload.update(stage="think", compact_confirmed=True)
            await self._render_model_switch_async(request)
            return
        if operation != "think" or request.payload["stage"] != "think":
            await self._complete_model_switch_reply_async(
                action, request, "Complete the current /model selection step first.", preserve_panel=True,
            )
            return
        status = llm.thinking_status(target_id)
        level = str(action.args.get("think_level") or status["current"])
        if level not in {choice["id"] for choice in status["choices"]}:
            await self._complete_model_switch_reply_async(
                action, request, "Invalid thinking level for the target model.", preserve_panel=True,
            )
            return
        try:
            lease = await self._host.begin_context_change("model_switch")
        except ControlBusyError as exc:
            scope.remove(action.route.control_scope_key, "model_switch")
            await self._complete_model_switch_reply_async(
                action, request, f"Model switching is unavailable while {exc}. Try again later.")
            return
        request.payload["stage"] = "executing"
        scope.set_executing(request, True)

        def current() -> bool:
            return (scope.get(action.route.control_scope_key, "model_switch") is request
                    and lease.current()
                    and request.payload["identity"] == self._model_selection_identity(llm, target_id)
                    and llm.settings_repository.get_active_llm_endpoint_id() == llm.active_endpoint_id)

        compact_result = None
        try:
            advice = self._model_switch_advice(llm, target_id, level)
            if advice.compact_required and not request.payload["compact_confirmed"]:
                request.payload["stage"] = "confirm"
                scope.set_executing(request, False)
                await self._render_model_switch_async(request, status)
                return
            await self._host.user_activity()
            if advice.compact_required:
                source = llm.active_endpoint_id
                if not source:
                    raise ValueError("Select the previous model first to Compact this history, or use /reset.")
                await self._host.deliver(interactions.terminal_delivery_for_action(
                    action, "Compacting on the current model before switching…",
                    delivery_kind="interactive_update",
                ))
                await self._host.flush(action.route)
                compact_result = await lease.compact(preferred_endpoint_id=source,
                    target_input_budget=advice.target_input_budget,
                    reserved_output_tokens=advice.reserved_output_tokens,
                    guard=current, force_semantic=True,
                )
                if not compact_result.success:
                    await self._complete_model_switch_reply_async(
                        action, request,
                        f"Model switch failed: Compact did not complete ({compact_result.status}); the model was not changed.\n"
                        + compact_result.user_notice,
                        preserve_panel=scope.get(action.route.control_scope_key, "model_switch") is not request,
                    )
                    return
            def apply_selection() -> None:
                if self._model_switch_advice(llm, target_id, level).compact_required:
                    raise ValueError("The current context still cannot be used by the target model.")
                llm.apply_model_selection(target_id, level)
                scope.remove(action.route.control_scope_key, "model_switch")
            await lease.commit(apply_selection, guard=current)
        except Exception as exc:
            suffix = (
                "\nThe completed Compact remains in effect.\n" + compact_result.user_notice
                if compact_result and compact_result.success else ""
            )
            await self._complete_model_switch_reply_async(
                action, request, f"Model switch failed: {exception_reason(exc)}{suffix}",
                preserve_panel=scope.get(action.route.control_scope_key, "model_switch") is not request,
            )
        else:
            # Delivery failure cannot roll back an already committed selection.
            # Let the channel error propagate without reporting a switch failure.
            message = f"Model updated to {target_id}. Thinking level: {level}. This applies to new turns only."
            if compact_result and compact_result.user_notice:
                message += "\n" + compact_result.user_notice
            await self._complete_model_switch_reply_async(action, request, message)
        finally:
            if request.payload.get("stage") == "executing" and scope.get(action.route.control_scope_key, "model_switch") is request:
                scope.remove(action.route.control_scope_key, "model_switch")
            try:
                if compact_result and compact_result.success:
                    await self._host.deliver_compact_candidates(action, compact_result.memory_result)
            finally:
                scope.set_executing(request, False)
                await lease.close()

    def _discard_model_selection_draft(self, scope_key: str) -> None:
        scope = self._host.requests
        request = scope.get(scope_key, "model_switch")
        if request is not None and request.payload.get("stage") != "executing":
            scope.remove(scope_key, "model_switch")

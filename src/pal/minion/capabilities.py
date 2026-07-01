from __future__ import annotations

import contextlib
import asyncio
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from uuid import uuid4

from pal.behavior.decorators import affordance
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.control import ControlAction
from pal.minion.interactions import (
    minion_question_answers,
    minion_question_ready,
    minion_question_resolve_delivery,
    minion_question_session,
    minion_question_session_with_selection,
    minion_question_update_delivery,
)
from pal.execution.contracts import CapabilityCall, CapabilityResult
from pal.foundation import BoundedTTLBuffer, utc_now
from pal.foundation.sidecar import pack_sidecar_message, read_sidecar_message
from pal.memory.candidates import l3_commit_args_from_memory_candidate
from pal.minion.capability_args import (
    normalize_top_level_review_gate_args,
    validate_draft_work_order_args,
    validate_promote_work_order_args,
)
from pal.minion.config import effective_minion_runtime_config, merge_minion_runtime_config
from pal.minion.dag_producer import (
    build_generic_single_node_plan_artifact,
    dag_producer_profile_for_family,
    resolve_default_executor_profile,
)
from pal.minion.ipc import MinionManagerClient, minion_log_path, minion_port_path, open_manager_connection, python_subprocess_env
from pal.minion.profiles import MinionProfileRegistry
from pal.minion.prompt import TaskingPromptFragmentProvider
from pal.minion.repository import MinionTaskingRepository
from pal.minion.skills import (
    PAL_MINION_DEVELOPMENT_SKILL_ID,
    PAL_MINION_PROFILE_DEVELOPMENT_SKILL_ID,
    minion_declared_skills,
)
from pal.minion.source import MinionControlEventHandler, MinionEventSource
from pal.minion.validation import MinionWorkOrderValidationError
from pal.minion.workflow import (
    DEFAULT_ARCHITECT_PROFILE,
    append_workflow_step,
    split_profile_ref,
)
from pal.minion.work_order import build_planner_work_order, new_work_id, prompt_view_from_metadata
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    EventKind,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    TaskContextPack,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


MINION_OBSERVATION_TTL_SECONDS = 6 * 60 * 60


@dataclass(frozen=True)
class MinionSnapshot:
    mounted: bool = True
    degraded: bool = False
    manager_running: bool = False
    active_count: int = 0
    run_count: int = 0
    pending_event_count: int = 0


class MinionIntrospection(Protocol):
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        ...


@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:minion",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="minion",
    kind="module",
    source="builtin:minion",
    target_kind="module",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="minion_task",
    path_module_id="minion_task",
    kind="module",
    source="builtin:minion",
    target_kind="task",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="minion_work_order",
    path_module_id="minion_work_order",
    kind="module",
    source="builtin:minion",
    target_kind="work_order",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="minion_work_order_draft",
    path_module_id="minion_work_order_draft",
    kind="module",
    source="builtin:minion",
    target_kind="work_order_draft",
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="minion_task",
    path_module_id="minion_task",
    kind="module",
    source="builtin:minion",
    target_kind="task",
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="minion",
    kind="module",
    source="builtin:minion",
    target_kind="module",
)
@affordance(
    affordance_id="declared.minion.delegate_professional_work",
    title="Delegate professional work to Minion",
    scenario_text=(
        "Use Minion for professional, asynchronous, module-scoped work that can be captured as SPEC, work order, "
        "and milestones. Good fits include code implementation, repository research, test repair, module review, "
        "long-running investigation, and task handoff where user-personal immediate judgment is weak."
    ),
    prompt_hint=(
        "Consider Minion when the task is professional, async-friendly, and can be bounded by SPEC/work order/milestones. "
        "Pal remains responsible for user interaction, fact checks, progress reporting, and confirmation. "
        "Before dispatch, search existing tasks with intro_minion_task_search; reuse a matching task_id or create one with "
        "op_minion_task_create so profile_family is bound once at the long-lived task layer. Then dispatch with "
        "op_minion_dispatch_workflow(task_id=...). Pal owns requirements shaping, and the manager owns internal profile "
        "dispatch, plan review, plan acceptance control, and module dispatch. Use intro_minion_profile_list/read only when "
        "the user asks about available profiles or requests a specific profile. "
        "Do not use Minion for casual chat, simple Q&A, one-call capabilities, "
        "memory/preference correction, or tasks needing continuous user interaction."
    ),
    activation_terms=(
        "minion",
        "delegate",
        "asynchronous",
        "async",
        "professional",
        "module",
        "module-scoped",
        "spec",
        "work order",
        "milestone",
        "research",
        "implementation",
        "test repair",
        "long-running",
        "handoff",
        "planner",
        "profile",
        "role",
        "draft",
    ),
    capability_refs=(
        "intro_minion_task_search",
        "intro_minion_task_read",
        "op_minion_task_create",
        "op_minion_dispatch_workflow",
        "intro_minion_profile_list",
        "intro_minion_profile_read",
        "intro_minion_plan_search",
        "intro_minion_plan_read",
        "intro_minion_list",
        "intro_minion_read",
        "op_minion_configure",
        "intro_minion_work_order_search",
        "intro_minion_work_order_read",
        "op_minion_recover_work_order",
        "op_minion_destroy_work_order_run",
        "op_minion_submit_repair_bill",
    ),
    priority=90,
    activation_threshold=0.35,
    metadata={"route_family": "minion", "resident": False},
)
@affordance(
    affordance_id="declared.minion.natural_language_takeover",
    title="Control or inspect active Minion work",
    scenario_text=(
        "Use this when the user asks what a minion is doing, asks progress, says to replace it, continue a task, "
        "kill a worker, or finalize completed minion work."
    ),
    prompt_hint=(
        "For minion progress/control requests, inspect minion and work-order facts first. Use intro_minion_list/read "
        "and intro_minion_work_order_search/read; do not infer progress or current worker from chat. If the user says to recover stale "
        "running_module state, use op_minion_recover_work_order. If the user says to replace or destroy a run, resolve the active run "
        "and work order, call op_minion_destroy_work_order_run, then use the work-order control path when appropriate. "
        "A task may have a parent work order plus one active module child; inspect the work-order fact snapshot before finalizing."
    ),
    activation_terms=(
        "minion",
        "worker",
        "work order",
        "progress",
        "doing",
        "status",
        "replace",
        "swap",
        "kill",
        "continue",
        "resume",
        "finalize",
        "merge",
        "换掉",
        "继续",
        "进度",
        "在干嘛",
        "合并",
    ),
    capability_refs=(
        "intro_minion_list",
        "intro_minion_read",
        "intro_minion_work_order_search",
        "intro_minion_work_order_read",
        "op_minion_kill",
        "op_minion_configure",
        "op_minion_destroy_work_order_run",
        "op_minion_recover_work_order",
        "op_minion_tick_parent_dag",
        "op_minion_finalize",
        "op_minion_submit_repair_bill",
    ),
    priority=95,
    activation_threshold=0.3,
    metadata={"route_family": "minion", "resident": False},
)
@affordance(
    affordance_id="declared.skill.pal_minion_profile_development",
    title="Pal minion profile development skill",
    scenario_text=(
        "The user wants to create, repair, review, or explain a Minion profile TOML file, profile capability groups, "
        "workspace policy, gate policy, output policy, workflow_next, or runtime profile override."
    ),
    prompt_hint=(
        "If this route is selected, inject skill `pal.minion.profile.development` before creating or changing Minion "
        "profile TOML, capability_groups, workspace_policy, capability_policy, gate_policy, output_policy, or workflow_next."
    ),
    activation_terms=(
        "new minion profile",
        "create minion profile",
        "build minion profile",
        "minion profile toml",
        "profile_group",
        "profile_id",
        "capability_groups",
        "capability_policy",
        "workspace_policy",
        "workspace_environment",
        "output_policy",
        "workflow_next",
        "profile_only",
        "inherit_filtered",
        "runtime profile",
        "plugins/minion/profiles",
        "构建 profile",
        "新 profile",
        "profile 模板",
        "profile toml",
    ),
    skill_refs=(PAL_MINION_PROFILE_DEVELOPMENT_SKILL_ID,),
    priority=45,
    activation_threshold=0.2,
    metadata={"skill_trigger": True, "resident": False, "extension_boundary": "minion.profiles"},
)
@affordance(
    affordance_id="declared.skill.pal_minion_development",
    title="Pal minion development skill",
    scenario_text=(
        "The user wants to add, repair, organize, or explain Minion workflow dispatch, profiles, scheduler/concurrency, "
        "coroutine runner behavior, workspace environment setup, repair bill replay, gate policy, reviewer gates, or gate ledgers."
    ),
    prompt_hint=(
        "If this route is selected, inject skill `pal.minion.development` before changing Minion workflow, profiles, "
        "scheduler/resource slots, coroutine runner behavior, workspace environment setup, repair bill replay, gate definitions, "
        "profile gate_policy wiring, reviewer strategy behavior, or repair ledger projection."
    ),
    activation_terms=(
        "minion development",
        "minion workflow",
        "dispatch_workflow",
        "workflow_next",
        "minion profile",
        "minion scheduler",
        "resource slot",
        "coroutine runner",
        "repair bill",
        "repair replay",
        "workspace environment",
        "minion gate",
        "gate policy",
        "gate definition",
        "GateDefinition",
        "GateChecklistEntry",
        "GateSpec",
        "checkpoint_quality",
        "plan_acceptance",
        "reviewer gate",
        "repair loop",
        "gate ledger",
        "active_gate_todo",
        "acceptance checklist",
        "minion gate policy",
        "reviewer repair",
        "门禁",
        "gate 策略",
        "reviewer gate",
        "检查清单",
        "minion 调度",
        "协程 runner",
        "修复账单",
    ),
    skill_refs=(PAL_MINION_DEVELOPMENT_SKILL_ID,),
    priority=40,
    activation_threshold=0.2,
    metadata={"skill_trigger": True, "resident": False, "extension_boundary": "minion"},
)
@dataclass
class MinionManagerProvider:
    runtime_root: Path
    context: MainContext | None = None
    mounted: bool = True
    degraded: bool = False
    process: subprocess.Popen | None = None
    last_error: str = ""
    last_health: dict[str, Any] = field(default_factory=dict)
    _buffered_events: list[dict[str, Any]] = field(default_factory=list)
    _buffer_lock: threading.Lock = field(default_factory=threading.Lock)
    _seen_event_keys: set[str] = field(default_factory=set)
    _event_subscription_stop: threading.Event = field(default_factory=threading.Event)
    _event_subscription_thread: threading.Thread | None = None
    _event_subscription_active: bool = False
    event_notify: Any | None = None
    recent_observations: BoundedTTLBuffer[dict[str, Any]] = field(
        default_factory=lambda: BoundedTTLBuffer(capacity=10, ttl_seconds=MINION_OBSERVATION_TTL_SECONDS)
    )
    client: MinionManagerClient = field(init=False)

    def __post_init__(self) -> None:
        self.client = MinionManagerClient(runtime_root=self.runtime_root)

    def declared_skills(self):
        return minion_declared_skills(module_id="minion")

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show", description="Show minion manager status")
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self._status_payload()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="minion manager status",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Minion manager status", payload),
        )

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="minion", action_name="list", description="List minion runs")
    def list_runs(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        result = self._request_or_error("list_runs")
        return _introspection_from_rpc("Minion runs", result)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion",
        action_name="read",
        description="Read one minion run",
        args_schema={"type": "object", "properties": {"run_id": {"type": "string"}}, "required": ["run_id"]},
    )
    def read_run(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self._request_or_error("read_run", {"run_id": str(call.args.get("run_id") or "")})
        return _introspection_from_rpc("Minion run", result)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion_task",
        action_name="search",
        description="Search minion tasks by natural language",
        args_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    )
    def search_tasks(self, call: IntrospectionCall) -> IntrospectionResult:
        payload = self._repository().search_tasks(str(call.args.get("query") or ""), limit=int(call.args.get("limit") or 10))
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="minion task search",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Minion task search", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion_task",
        action_name="read",
        description="Read a minion task fact snapshot",
        args_schema={"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    )
    def read_task(self, call: IntrospectionCall) -> IntrospectionResult:
        payload = self._repository().read_task(str(call.args.get("task_id") or ""))
        return _introspection_from_rpc("Minion task", payload)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion_work_order",
        action_name="search",
        description="Search minion work orders by natural language",
        args_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    )
    def search_work_orders(self, call: IntrospectionCall) -> IntrospectionResult:
        payload = self._repository().search_work_orders(str(call.args.get("query") or ""), limit=int(call.args.get("limit") or 10))
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="minion work order search",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Minion work order search", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion_work_order_draft",
        action_name="search",
        description="Search minion work order drafts by natural language",
        args_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    )
    def search_work_order_drafts(self, call: IntrospectionCall) -> IntrospectionResult:
        payload = self._repository().search_work_order_drafts(str(call.args.get("query") or ""), limit=int(call.args.get("limit") or 10))
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="minion work order draft search",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Minion work order draft search", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion_work_order_draft",
        action_name="read",
        description="Read a minion work order draft and its candidate work order packet",
        args_schema={
            "type": "object",
            "properties": {"draft_id": {"type": "string"}},
            "required": ["draft_id"],
        },
    )
    def read_work_order_draft(self, call: IntrospectionCall) -> IntrospectionResult:
        payload = self._repository().read_work_order_draft(str(call.args.get("draft_id") or ""))
        return _introspection_from_rpc("Minion work order draft", payload)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion_work_order",
        action_name="read",
        description="Read a minion work order fact snapshot including milestones and current worker",
        args_schema={
            "type": "object",
            "properties": {"work_order_id": {"type": "string"}},
            "required": ["work_order_id"],
        },
    )
    def read_work_order(self, call: IntrospectionCall) -> IntrospectionResult:
        payload = self._repository().read_work_order(
            str(call.args.get("work_order_id") or ""),
            active_runs=self._active_runs_snapshot(),
        )
        return _introspection_from_rpc("Minion work order", payload)

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="minion", action_name="profile_list", description="List minion profiles")
    def list_profiles(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        profiles = [_profile_summary_payload(profile.to_dict()) for profile in self._profile_registry().list_profiles()]
        payload = {
            "profiles": profiles,
            "count": len(profiles),
            "runtime_profile_dir": str(Path(self.runtime_root) / "plugins" / "minion" / "profiles"),
            "runtime_profile_pattern": "runtime_root/plugins/minion/profiles/**/*.toml",
            "profile_source_order": ["builtin package TOML", "runtime TOML", "mounted provider declarations"],
            "usage": "Use intro_minion_task_search/op_minion_task_create before op_minion_dispatch_workflow. Dispatch uses task.profile_family; it does not take profile selectors.",
        }
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="minion profiles",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Minion profiles", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion",
        action_name="profile_read",
        description="Read one minion profile",
        args_schema={
            "type": "object",
            "properties": {
                "profile_group": {"type": "string"},
                "profile_name": {"type": "string"},
                "profile_id": {"type": "string", "description": "Legacy/internal canonical lookup string; prefer profile_group/profile_name."},
            },
        },
    )
    def read_profile(self, call: IntrospectionCall) -> IntrospectionResult:
        profile_group = str(call.args.get("profile_group") or "").strip()
        profile_name = str(call.args.get("profile_name") or "").strip()
        profile_id = str(call.args.get("profile_id") or "").strip()
        profile = (
            self._profile_registry().get_ref(profile_group, profile_name)
            if profile_group or profile_name
            else self._profile_registry().get(profile_id)
        )
        if profile is None:
            payload = {"profile_group": profile_group, "profile_name": profile_name, "profile_id": profile_id, "error": "unknown minion profile"}
            return IntrospectionResult(
                status=RuntimeStatus.NOT_FOUND,
                text="minion profile not found",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Minion profile not found", payload),
            )
        payload = profile.to_dict()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="minion profile",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Minion profile", payload),
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion",
        action_name="plan_search",
        description="Search first-class minion plan refs by task, plan, module, or summary text",
        args_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}},
        },
    )
    def search_plans(self, call: IntrospectionCall) -> IntrospectionResult:
        payload = self._repository().search_plan_refs(
            str(call.args.get("query") or ""),
            limit=int(call.args.get("limit") or 10),
        )
        return _introspection_from_rpc("Minion plans", payload)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion",
        action_name="plan_read",
        description="Read and validate a first-class minion plan_ref",
        args_schema={"type": "object", "properties": {"plan_ref": {"type": "object"}}, "required": ["plan_ref"]},
    )
    def read_plan(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            payload = self._repository().read_plan_ref(call.args.get("plan_ref"))
            return _introspection_from_rpc("Minion plan", payload)
        except Exception as exc:
            payload = {"status": "invalid", "error": f"{exc.__class__.__name__}: {exc}"}
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="minion plan invalid",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Minion plan invalid", payload),
            )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="minion", family="minion", action_name="attach", description="Attach minion manager")
    def attach(self, call: IntrospectionCall | None = None) -> IntrospectionResult:
        _ = call
        self.mounted = True
        self.degraded = False
        try:
            self._ensure_manager_started()
        except Exception as exc:
            self.degraded = True
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            payload = self._status_payload()
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text="minion manager attach failed",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Minion manager attach failed", payload),
            )
        payload = self._status_payload()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="minion manager attached",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Minion manager attached", payload),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="minion", family="minion", action_name="detach", description="Detach minion manager")
    def detach(self, call: IntrospectionCall | None = None) -> IntrospectionResult:
        _ = call
        self._stop_manager(force=True)
        self.mounted = False
        self.degraded = False
        payload = self._status_payload()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="minion manager detached",
            structured=payload,
            llm_text=render_titled_structured_for_llm("Minion manager detached", payload),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        family="minion",
        action_name="configure",
        description=(
            "Configure Minion manager runtime behavior. runner_mode defaults to coroutine; set runner_mode=process only as a "
            "fallback. Configuration is persisted in Minion's own runtime database under data/minion/minion.sqlite3. If the "
            "manager is currently running with active work, the new setting is recorded and takes effect after manager restart."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "runner_mode": {
                    "type": "string",
                    "enum": ["coroutine", "process"],
                    "description": "Execution backend for minion runners. Default is coroutine.",
                },
                "max_parallel_modules": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Global module slot limit.",
                },
                "auto_resume_ready_modules": {
                    "type": "boolean",
                    "description": "Whether manager auto-starts ready modules after dependency completion.",
                },
                "apply_now": {
                    "type": "boolean",
                    "default": True,
                    "description": "Restart an idle manager immediately so the new setting is effective.",
                },
            },
        },
    )
    def configure(self, call: CapabilityCall) -> CapabilityResult:
        try:
            args = dict(call.args or {})
            patch = {
                key: args[key]
                for key in ("runner_mode", "max_parallel_modules", "auto_resume_ready_modules")
                if key in args
            }
            config = merge_minion_runtime_config(self.runtime_root, patch) if patch else effective_minion_runtime_config(self.runtime_root)
            apply_now = bool(args.get("apply_now", True))
            manager_running = self.process is not None and self.process.poll() is None
            active_runs = self._has_active_runs_sync() if manager_running else False
            applied_now = False
            restart_required = bool(manager_running and active_runs)
            if apply_now and manager_running and not active_runs:
                self._stop_manager(force=True)
                self._ensure_manager_started()
                applied_now = True
                restart_required = False
            payload = {
                "status": "ok",
                "config": config,
                "applied_now": applied_now,
                "restart_required": restart_required,
                "manager_running": manager_running,
                "active_runs": active_runs,
            }
            return _capability_from_rpc("minion manager configured", payload)
        except ValueError as exc:
            return _capability_invalid("minion manager configuration invalid", MinionWorkOrderValidationError(str(exc), field="minion_config"))
        except Exception as exc:
            return _capability_error("minion manager configuration failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        family="minion",
        action_name="dispatch_workflow",
        description=(
            "Normal public entrypoint for Minion delegation. Pal's main agent owns requirements shaping; pass the prepared user "
            "intent, task_id, requirements_brief, and workspace facts here. Search/create a Minion task first; task.profile_family "
            "selects how the DAG is interpreted. Dispatch does not accept profile selectors. The manager either runs the family DAG "
            "producer or uses the generic single-node DAG producer, then consumes the resulting DAG mechanically. Do not call lower-level "
            "plan/spawn capabilities. "
            "architecture_mode only affects the default software architect step: auto conservatively chooses micro/full from workspace "
            "kind, goal scope, repo scan hints, and explicit user hints. micro asks the architect for a small canonical plan, normally "
            "one implementation module plus a final verification join, with a prelude only when real shared setup/contracts are needed. "
            "full asks for a complete multi-module plan. If micro scope is unsafe, the architect must escalate instead of weakening contracts. "
            "interaction_mode controls user confirmation: interactive asks for plan acceptance and disables module auto-advance; "
            "auto_after_plan asks for plan acceptance, then auto-advances modules when gates pass; autonomous auto-accepts reviewed "
            "plans and asks only for failures, permissions, destructive/high-risk actions, or missing user-owned facts. "
            "workspace.kind is new_project or existing_repo. software_engineering new projects must include primary_language; "
            "non-software artifact/profile tasks may omit it. Existing repos should "
            "include repo_path or cwd; manager records origin_repo_path and prepares durable minion worktrees under runtime data."
        ),
        aliases=(
            "dispatch minion",
            "launch minion",
            "run minion",
            "start minion",
            "send minion",
            "execute minion work order",
            "dispatch work order",
            "launch work order",
            "run work order",
        ),
        args_schema={
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": (
                        "Existing Minion task id. Call intro_minion_task_search first and op_minion_task_create when no matching task exists."
                    ),
                },
                "goal": {"type": "string", "description": "User-visible outcome Minion should plan and execute."},
                "requirements_brief": {
                    "type": "object",
                    "description": (
                        "Optional requirements brief prepared by Pal's main agent. Use this for scope, acceptance criteria, constraints, "
                        "open questions already resolved, and user preferences. Minion no longer runs a separate requirements planner."
                    ),
                },
                "workspace": {
                    "type": "object",
                    "description": (
                        "Intent-level workspace facts. Use kind=new_project with primary_language for software 0-1 work, or "
                        "kind=existing_repo with repo_path/cwd for repo changes. Non-software artifact/profile tasks may use "
                        "kind=new_project without primary_language. Do not provide runtime worktree paths; "
                        "manager allocates them under runtime_root/data/minion/repos."
                    ),
                    "properties": {
                        "kind": {"type": "string", "enum": ["new_project", "existing_repo"]},
                        "repo_path": {"type": "string"},
                        "cwd": {"type": "string"},
                        "project_name": {"type": "string"},
                        "primary_language": {"type": "string"},
                        "languages": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "architecture_mode": {
                    "type": "string",
                    "enum": ["auto", "micro", "full"],
                    "default": "auto",
                    "description": (
                        "auto lets manager choose micro/full conservatively. micro still uses architect and must produce a canonical "
                        "plan with acceptance criteria and test strategy. full uses architect for a complete multi-module plan."
                    ),
                },
                "interaction_mode": {
                    "type": "string",
                    "enum": ["interactive", "auto_after_plan", "autonomous"],
                    "default": "auto_after_plan",
                    "description": (
                        "interactive asks for plan acceptance and disables module auto-advance. auto_after_plan asks for plan "
                        "acceptance, then auto-advances modules. autonomous auto-accepts passing plan review and only asks on "
                        "failures, permissions, destructive/high-risk actions, or missing user-owned facts."
                    ),
                },
                "preferred_endpoint_id": {
                    "type": "string",
                    "description": (
                        "Optional enabled LLM endpoint_id to use for this minion. Fill only when the user explicitly names a model "
                        "or endpoint for the minion; otherwise omit it so the runner follows Pal's active endpoint setting."
                    ),
                },
                "title": {"type": "string", "description": "Optional short display title for the workflow."},
                "approval_policy": {"type": "object"},
            },
            "required": ["task_id", "goal", "workspace"],
        },
        metadata={
            "search_priority": 50,
            "normal_minion_entrypoint": True,
        },
    )
    def dispatch_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            repository = self._repository()
            args = _workflow_args_with_task_family(dict(call.args or {}), repository=repository)
            profile_registry = self._profile_registry()
            pack, workflow_payload = _workflow_entry_pack_from_args(
                args,
                repository=repository,
                profile_registry=profile_registry,
            )
            pack = self._inject_control_route(pack, call)
            pack = self._inject_debug_log_request(pack, call)
            pack = self._inject_preferred_endpoint(pack, call)
            pack = profile_registry.resolve_pack(
                pack,
                requested_profile_group=str(pack.profile_group or ""),
                requested_profile_name=str(pack.profile_name or ""),
            )
            pack = repository.prepare_pack_for_spawn(pack)
            self._ensure_manager_started()
            result = self.client.spawn_sync(pack.to_dict())
            self.last_health = dict(result)
            payload = {
                "status": str(result.get("status") or "ok"),
                "workflow_id": workflow_payload["workflow_id"],
                "work_order_id": pack.work_order_id,
                "run_id": str(result.get("run_id") or ""),
                "minion_id": str(result.get("minion_id") or ""),
                "run": dict(result),
                "workspace_summary": workflow_payload["workspace_summary"],
                "architecture_mode": workflow_payload["architecture_mode"],
                "requested_architecture_mode": workflow_payload["requested_architecture_mode"],
                "interaction_mode": workflow_payload["interaction_mode"],
                "initial_profile": workflow_payload["initial_profile"],
                "profile_family": workflow_payload["profile_family"],
                "next_action": workflow_payload["next_action"],
            }
            return _capability_from_rpc("minion workflow dispatched", payload)
        except MinionWorkOrderValidationError as exc:
            return _capability_invalid("minion workflow dispatch invalid", exc)
        except Exception as exc:
            return _capability_error("minion workflow dispatch failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion_task",
        family="minion",
        action_name="create",
        description=(
            "Create or update a long-lived Minion task after searching for an existing matching task. "
            "Task is the durable semantic container and binds profile_family; work orders are execution attempts under it. "
            "Call intro_minion_task_search first with the user goal/repo/domain and reuse a matching task_id instead of creating duplicates."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Optional stable task id. Omit to allocate one."},
                "title": {"type": "string", "description": "Short task title."},
                "goal": {"type": "string", "description": "Long-lived task goal or domain purpose."},
                "summary": {"type": "string", "description": "Searchable task summary, constraints, repo/domain facts, or scope."},
                "profile_family": {
                    "type": "string",
                    "description": (
                        "Required profile family that interprets this task's work orders. Use software_engineering for code/repo/review "
                        "work; use lifestyle for nutrition, diet, meal-plan, training, health check-in, or similar personal-life coaching "
                        "tasks. Use general only when no domain family fits."
                    ),
                },
                "workspace": {
                    "type": "object",
                    "description": "Optional durable task-level workspace facts such as repo_path/origin_repo_path, project_name, or domain context.",
                },
                "metadata": {"type": "object"},
            },
            "required": ["goal", "profile_family"],
        },
        metadata={"omit_family_in_canonical": True},
    )
    def create_task(self, call: CapabilityCall) -> CapabilityResult:
        try:
            args = dict(call.args or {})
            profile_family = _profile_ref_text(args.get("profile_family") or args.get("family"))
            if not profile_family:
                raise MinionWorkOrderValidationError("profile_family is required", field="profile_family")
            args["profile_family"] = profile_family
            payload = self._repository().create_task(args)
            return _capability_from_rpc("minion task created", payload)
        except MinionWorkOrderValidationError as exc:
            return _capability_invalid("minion task invalid", exc)
        except ValueError as exc:
            return _capability_invalid("minion task invalid", MinionWorkOrderValidationError(str(exc), field="task"))
        except Exception as exc:
            return _capability_error("minion task create failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        family="minion",
        action_name="review_gate_submit",
        description=(
            "Submit a structured reviewer/verifier gate result. Use this from reviewer or verifier minions after checking a plan, "
            "checkpoint, or repair target. The manager/repository validates target binding and records the gate in the ledger."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "gate_kind": {
                    "type": "string",
                    "enum": ["plan_acceptance", "checkpoint_verification", "repair_verification"],
                },
                "target": {"type": "object"},
                "verdict": {"type": "string", "enum": ["pass", "fail", "partial"]},
                "summary": {"type": "string"},
                "findings": {"type": "array", "items": {"type": "object"}},
                "required_fixes": {"type": "array", "items": {"type": "object"}},
                "evidence": {"type": "array", "items": {"type": "object"}},
                "commands_run": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "For checkpoint/repair pass verdicts, include at least one command/check entry with command, cwd, status or exit_code, output summary, and covers=[exact acceptance criteria or refs]. The command field must be copied exactly from one op_exec_shell args.cmd string in this reviewer run; do not summarize, shorten, normalize, or combine commands.",
                },
                "api_evidence": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Source/docs/LSP/build evidence for API and call-shape claims. Pass verdicts require api_evidence or metadata.api_evidence_not_applicable=true with a reason. Include covers=[exact acceptance criteria or refs] when evidence proves milestone acceptance.",
                },
                "residual_risk": {"type": "array", "items": {"type": "object"}},
                "report_artifact_ref": {"type": "object"},
                "reviewer_profile": {"type": "string"},
                "metadata": {
                    "type": "object",
                    "description": (
                        "Use metadata.api_evidence_not_applicable=true only when API evidence is genuinely not applicable. "
                        "If no LSP evidence is used on a pass verdict, set metadata.lsp_evidence_not_applicable=true with a reason. "
                        "Top-level pass gates are external gates only and require metadata.external_verification_ref. "
                        "Human overrides are control/UI actions only."
                    ),
                },
            },
            "required": ["gate_kind", "target", "verdict"],
        },
    )
    def review_gate_submit(self, call: CapabilityCall) -> CapabilityResult:
        try:
            args = normalize_top_level_review_gate_args(dict(call.args or {}), repository=self._repository())
            payload = self._repository().submit_review_gate(
                args,
                reviewer_profile=str(args.get("reviewer_profile") or ""),
                work_order_id=str(args.get("work_order_id") or ""),
                run_id=str(args.get("run_id") or ""),
            )
            return _capability_from_rpc("minion review gate recorded", payload)
        except MinionWorkOrderValidationError as exc:
            return _capability_invalid("minion review gate invalid", exc)
        except ValueError as exc:
            return _capability_invalid("minion review gate invalid", MinionWorkOrderValidationError(str(exc), field="review_gate"))
        except Exception as exc:
            return _capability_error("minion review gate failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        family="minion",
        action_name="draft_work_order",
        description=(
            "Draft a minion work order candidate from user brainstorming or module-boundary discussion. "
            "Prefer structured module plans, prompt_view, and acceptance criteria over dumping raw conversation or payload JSON."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short work-order title."},
                "goal": {"type": "string", "description": "User-visible outcome the minion must achieve."},
                "instruction": {"type": "string", "description": "Concrete handoff instructions, not just the goal repeated."},
                "source_summary": {"type": "string", "description": "Facts, files, commands, or evidence that justify the work order."},
                "conversation_summary": {"type": "string", "description": "Short relevant user context, only when needed."},
                "module_boundaries": {
                    "description": "Positive module ownership boundaries and scoped responsibilities.",
                },
                "milestones": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"oneOf": [{"type": "string"}, {"type": "object"}]},
                    "description": "Ordered, concrete milestones the minion should report against.",
                },
                "acceptance_criteria": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Observable checks that define done.",
                },
                "workspace": {
                    "type": "object",
                    "description": "Repo, cwd, branch, runtime, or environment facts if applicable. For local repo tasks prefer repo_path; cwd with type=local_repo is accepted as fallback.",
                },
                "artifacts": {"type": "array", "description": "Relevant files, logs, links, images, or previous outputs."},
                "task_id": {"type": "string"},
                "task_title": {"type": "string", "description": "Parent task title if different from work-order title."},
                "proposed_work_order_id": {"type": "string"},
                "profile_family": {
                    "type": "string",
                    "description": "Profile family for this draft, such as software_engineering. If profile_name is omitted, drafts the family architect profile.",
                },
                "profile_group": {
                    "type": "string",
                    "description": "Exact profile group when selecting a specific profile with profile_name.",
                },
                "profile_name": {
                    "type": "string",
                    "description": "Exact profile name within profile_family/profile_group.",
                },
                "minion_profile": {
                    "type": "string",
                    "description": "Registered canonical profile id. Prefer profile_family/profile_name unless a canonical id is already known.",
                },
                "metadata": {"type": "object"},
            },
            "required": [
                "goal",
                "milestones",
            ],
            "anyOf": [
                {"required": ["minion_profile"]},
                {"required": ["profile_family"]},
                {"required": ["profile_group", "profile_name"]},
            ],
        },
    )
    def draft_work_order(self, call: CapabilityCall) -> CapabilityResult:
        try:
            args = validate_draft_work_order_args(dict(call.args))
            payload = self._repository().create_work_order_draft(args)
            return _capability_from_rpc("minion work order draft created", payload)
        except MinionWorkOrderValidationError as exc:
            return _capability_invalid("minion work order draft invalid", exc)
        except Exception as exc:
            return _capability_error("minion work order draft failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        family="minion",
        action_name="promote_work_order_draft",
        description="Promote a reviewed minion work order draft into a formal work order without spawning a runner",
        args_schema={
            "type": "object",
            "properties": {
                "draft_id": {"type": "string"},
                "reviewed_candidate": {"type": "object"},
            },
            "required": ["draft_id"],
        },
    )
    def promote_work_order_draft(self, call: CapabilityCall) -> CapabilityResult:
        try:
            repository = self._repository()
            reviewed_candidate = dict(call.args.get("reviewed_candidate") or {}) or None
            validate_promote_work_order_args(repository, str(call.args.get("draft_id") or ""), reviewed_candidate=reviewed_candidate)
            payload = repository.promote_work_order_draft(
                str(call.args.get("draft_id") or ""),
                reviewed_candidate=reviewed_candidate,
            )
            return _capability_from_rpc("minion work order draft promoted", payload)
        except MinionWorkOrderValidationError as exc:
            return _capability_invalid("minion work order draft promotion invalid", exc)
        except Exception as exc:
            return _capability_error("minion work order draft promotion failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        family="minion",
        action_name="kill",
        description="Kill a minion run",
        args_schema={
            "type": "object",
            "properties": {"run_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["run_id"],
        },
    )
    def kill(self, call: CapabilityCall) -> CapabilityResult:
        try:
            self._ensure_manager_started()
            result = self.client.kill_sync(str(call.args.get("run_id") or ""), str(call.args.get("reason") or ""))
            return _capability_from_rpc("minion killed", result)
        except Exception as exc:
            return _capability_error("minion kill failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        family="minion",
        action_name="finalize",
        description="Finalize a minion work order by squashing milestone commits into the merge target",
        args_schema={
            "type": "object",
            "properties": {
                "work_order_id": {"type": "string"},
                "message": {"type": "string"},
            },
            "required": ["work_order_id"],
        },
    )
    def finalize(self, call: CapabilityCall) -> CapabilityResult:
        try:
            self._ensure_manager_started()
            result = self.client.finalize_work_order_sync(
                str(call.args.get("work_order_id") or ""),
                message=str(call.args.get("message") or ""),
            )
            return _capability_from_rpc("minion work order finalized", result)
        except Exception as exc:
            return _capability_error("minion finalize failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        family="minion",
        action_name="tick_parent_dag",
        description="Tick a minion parent DAG once so ready nodes can be claimed and dispatched through manager-owned slots",
        args_schema={
            "type": "object",
            "properties": {"work_order_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["work_order_id"],
        },
    )
    def tick_parent_dag(self, call: CapabilityCall) -> CapabilityResult:
        try:
            self._ensure_manager_started()
            result = self.client.request_sync(
                "tick_parent_dag",
                {
                    "work_order_id": str(call.args.get("work_order_id") or ""),
                    "reason": str(call.args.get("reason") or "capability"),
                },
            )
            return _capability_from_rpc("minion parent DAG ticked", result)
        except Exception as exc:
            return _capability_error("minion parent DAG tick failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        family="minion",
        action_name="submit_repair_bill",
        description=(
            "Submit a structured repair bill for a parent plan work order. The bill must reference existing module ids; "
            "module_defect and contract_defect replay the target module plus downstream dependents. "
            "architecture_defect blocks the parent and requires plan/module-boundary review before a replacement DAG epoch."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "parent_work_order_id": {"type": "string"},
                "source_module_id": {"type": "string"},
                "summary": {"type": "string"},
                "bill_id": {"type": "string"},
                "module_patches": {
                    "type": "object",
                    "description": "Map of existing module_id to repair patch.",
                },
                "modules": {
                    "type": "array",
                    "description": "Compatibility alias: list of existing module patch objects with module_id/module_key.",
                    "items": {"type": "object"},
                },
            },
            "required": ["parent_work_order_id"],
            "additionalProperties": True,
        },
    )
    def submit_repair_bill(self, call: CapabilityCall) -> CapabilityResult:
        try:
            self._ensure_manager_started()
            result = self.client.submit_repair_bill_sync(dict(call.args))
            return _capability_from_rpc("minion repair bill submitted", result)
        except Exception as exc:
            return _capability_error("minion repair bill submit failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        family="minion",
        action_name="recover_work_order",
        description=(
            "Recover stale minion parent work-order state after manager restart or runner loss. "
            "This does not complete a module; it releases stale running_module state so the current module can be retried."
        ),
        args_schema={
            "type": "object",
            "properties": {"work_order_id": {"type": "string"}, "reason": {"type": "string"}},
        },
    )
    def recover_work_order(self, call: CapabilityCall) -> CapabilityResult:
        try:
            self._ensure_manager_started()
            result = self.client.request_sync(
                "recover_work_order",
                {"work_order_id": str(call.args.get("work_order_id") or ""), "reason": str(call.args.get("reason") or "")},
            )
            return _capability_from_rpc("minion work order recovered", result)
        except Exception as exc:
            return _capability_error("minion work order recovery failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        family="minion",
        action_name="destroy_work_order_run",
        description=(
            "Destroy the active runner for a minion work order or its active child module and release the parent running_module state. "
            "Use for explicit recovery/teardown; this does not mark the module completed."
        ),
        args_schema={
            "type": "object",
            "properties": {"work_order_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["work_order_id"],
        },
    )
    def destroy_work_order_run(self, call: CapabilityCall) -> CapabilityResult:
        try:
            self._ensure_manager_started()
            result = self.client.request_sync(
                "destroy_work_order_run",
                {"work_order_id": str(call.args.get("work_order_id") or ""), "reason": str(call.args.get("reason") or "")},
            )
            return _capability_from_rpc("minion work order run destroyed", result)
        except Exception as exc:
            return _capability_error("minion work order run destroy failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        family="minion",
        action_name="pause_work_order",
        description="Pause a minion parent work order at its current milestone cursor",
        args_schema={
            "type": "object",
            "properties": {"work_order_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["work_order_id"],
        },
    )
    def pause_work_order(self, call: CapabilityCall) -> CapabilityResult:
        try:
            self._ensure_manager_started()
            result = self.client.request_sync(
                "pause_work_order",
                {"work_order_id": str(call.args.get("work_order_id") or ""), "reason": str(call.args.get("reason") or "")},
            )
            return _capability_from_rpc("minion work order paused", result)
        except Exception as exc:
            return _capability_error("minion work order pause failed", exc)

    async def handle_control_action_async(self, action: ControlAction) -> Any:
        if action.action_kind == "minion_lesson_decision":
            return await self._handle_lesson_decision_async(action)
        if action.action_kind in {"minion_question_select", "minion_question_nav", "minion_question_submit"}:
            return await self._handle_question_interaction_async(action)
        if action.action_kind == "minion_question_answer":
            return await self._handle_question_answer_async(action)
        if action.action_kind == "minion_plan_accept_override":
            return await self._handle_plan_accept_override_async(action)
        if action.action_kind in {"minion_plan_reject", "minion_plan_edit"}:
            return await self._handle_plan_review_decision_async(action)
        if action.action_kind in {"minion_dag_tick", "minion_plan_pause", "minion_plan_finish"}:
            return await self._handle_plan_control_async(action)
        if action.action_kind != "minion_approval_decision":
            return ""
        decision = str(action.args.get("decision") or "").strip().lower()
        approval_id = str(action.args.get("approval_id") or action.target_id or "").strip()
        payload = {
            "approval_id": approval_id,
            "decision": decision,
            "run_id": str(action.args.get("run_id") or ""),
            "minion_id": str(action.args.get("minion_id") or ""),
        }
        self._ensure_manager_started()
        result = await _to_thread(self.client.send_decision_sync, payload)
        status = str(result.get("status") or RuntimeStatus.OK)
        if status == RuntimeStatus.OK:
            if decision == "accept_all":
                return "Minion approval recorded; remaining approvals for this run will be accepted."
            return f"Minion approval {decision or 'decision'} recorded."
        return str(result.get("error") or "Minion approval decision failed.")

    async def _handle_plan_accept_override_async(self, action: ControlAction) -> str:
        plan_ref = action.args.get("plan_ref")
        if not plan_ref:
            return "Minion plan acceptance is missing plan_ref."
        reason = str(action.args.get("reason") or action.notes or "human accepted plan through control action").strip()
        human_override = {
            "reason": reason,
            "actor": str(action.args.get("actor") or "human").strip() or "human",
            "source": "control_action",
            "action_kind": action.action_kind,
            "target_scope": action.target_scope,
            "target_id": str(action.target_id or ""),
        }
        for key in ("interaction_origin", "interaction_id", "interaction_kind"):
            if str(action.args.get(key) or "").strip():
                human_override[key] = str(action.args.get(key) or "").strip()
        if action.route is not None:
            human_override["route"] = {
                "endpoint_id": action.route.endpoint_id,
                "channel_kind": action.route.channel_kind,
            }
        payload = self._repository().accept_plan_ref(
            plan_ref,
            reason=reason,
            review_gate_ref=action.args.get("review_gate_ref"),
            human_override=human_override,
        )
        work_order_id = str(action.args.get("work_order_id") or action.target_id or "").strip()
        if work_order_id:
            accepted_ref = dict(payload.get("plan_ref") or {})
            review_gate_ref = dict(action.args.get("review_gate_ref") or {}) if isinstance(action.args.get("review_gate_ref"), dict) else {}
            self._ensure_manager_started()
            dispatch = await _to_thread(
                self.client.request_sync,
                "dispatch_accepted_plan",
                {
                    "work_order_id": work_order_id,
                    "plan_ref": accepted_ref,
                    "review_gate_ref": review_gate_ref,
                },
            )
            self._repository().merge_work_order_metadata(
                work_order_id,
                {
                    "plan_review": {
                        "status": "accepted",
                        "plan_ref": accepted_ref,
                        "review_gate_ref": review_gate_ref,
                        "acceptance": payload,
                        "dispatch": dispatch,
                        "updated_at": utc_now(),
                        "next_action": "executing_plan",
                        "human_decision": human_override,
                    }
                },
            )
            self._repository().record_minion_event(
                {
                    "event_kind": "plan_accepted",
                    "work_order_id": work_order_id,
                    "minion_id": "",
                    "run_id": "",
                    "minion_profile": "control",
                    "payload": {
                        "status": "accepted",
                        "summary": "human accepted reviewed plan",
                        "plan_ref": accepted_ref,
                        "review_gate_ref": review_gate_ref,
                        "dispatch": dispatch,
                    },
                }
            )
        plan_id = str((payload.get("plan_ref") or {}).get("plan_id") or "")
        return f"Minion plan accepted by human override{f' ({plan_id})' if plan_id else ''}."

    async def _handle_plan_review_decision_async(self, action: ControlAction) -> str:
        plan_ref = action.args.get("plan_ref")
        if not isinstance(plan_ref, dict):
            return "Minion plan decision is missing plan_ref."
        work_order_id = str(action.args.get("work_order_id") or action.target_id or "").strip()
        if not work_order_id:
            return "Minion plan decision is missing work_order_id."
        decision = "rejected" if action.action_kind == "minion_plan_reject" else "edit_requested"
        review_gate_ref = dict(action.args.get("review_gate_ref") or {}) if isinstance(action.args.get("review_gate_ref"), dict) else {}
        review_state = {
            "status": decision,
            "plan_ref": dict(plan_ref),
            "review_gate_ref": review_gate_ref,
            "updated_at": utc_now(),
            "next_action": "revise_plan",
            "human_decision": {
                "action_kind": action.action_kind,
                "actor": str(action.args.get("actor") or "human").strip() or "human",
                "reason": str(action.args.get("reason") or action.notes or "").strip(),
            },
        }
        edit_instruction = str(action.args.get("edit_instruction") or "").strip()
        if edit_instruction:
            review_state["edit_instruction"] = edit_instruction
        for key in ("interaction_origin", "interaction_id", "interaction_kind"):
            if str(action.args.get(key) or "").strip():
                review_state["human_decision"][key] = str(action.args.get(key) or "").strip()
        self._repository().merge_work_order_metadata(work_order_id, {"plan_review": review_state})
        self._ensure_manager_started()
        revision = await _to_thread(
            self.client.request_sync,
            "dispatch_plan_revision",
            {
                "work_order_id": work_order_id,
                "plan_ref": dict(plan_ref),
                "review_gate_ref": review_gate_ref,
                "reason": str(action.args.get("reason") or action.notes or "").strip(),
                "edit_instruction": edit_instruction,
            },
        )
        review_state["revision_dispatch"] = revision
        review_state["next_action"] = "wait_for_revision" if str(revision.get("status") or "") == "spawned" else "revise_plan"
        self._repository().merge_work_order_metadata(work_order_id, {"plan_review": review_state})
        self._repository().record_minion_event(
            {
                "event_kind": "plan_rejected" if decision == "rejected" else "plan_edit_requested",
                "work_order_id": work_order_id,
                "minion_id": "",
                "run_id": "",
                "minion_profile": "control",
                "payload": {
                    "status": decision,
                    "summary": "human rejected reviewed plan" if decision == "rejected" else "human requested plan edit",
                    "plan_ref": dict(plan_ref),
                    "review_gate_ref": review_gate_ref,
                    "revision_dispatch": revision,
                    "next_action": review_state["next_action"],
                },
            }
        )
        plan_id = str(plan_ref.get("plan_id") or "")
        if decision == "rejected":
            return f"Minion plan rejected{f' ({plan_id})' if plan_id else ''}; revision architect dispatched."
        return f"Minion plan edit requested{f' ({plan_id})' if plan_id else ''}; revision architect dispatched."

    async def _handle_plan_control_async(self, action: ControlAction) -> str:
        work_order_id = str(action.args.get("work_order_id") or action.target_id or "").strip()
        if not work_order_id:
            return "Minion work order action is missing work_order_id."
        self._ensure_manager_started()
        if action.action_kind == "minion_dag_tick":
            result = await _to_thread(self.client.request_sync, "tick_parent_dag", {"work_order_id": work_order_id, "reason": "control_action"})
            if str(result.get("reason") or "") == "active_child_running":
                child_id = str(result.get("active_child_work_order_id") or "")
                return f"Minion work order already has an active module{f' ({child_id})' if child_id else ''}."
            if str(result.get("status") or "") == "running_module":
                module_id = str(result.get("module_id") or "")
                return f"Minion DAG tick started{f' {module_id}' if module_id else ''}."
            return f"Minion DAG tick result: {result.get('status') or 'unknown'}."
        if action.action_kind == "minion_plan_pause":
            result = await _to_thread(
                self.client.request_sync,
                "pause_work_order",
                {"work_order_id": work_order_id, "reason": str(action.args.get("reason") or "user paused at milestone boundary")},
            )
            return f"Minion work order paused ({result.get('status') or 'unknown'})."
        result = await _to_thread(
            self.client.request_sync,
            "finish_work_order",
            {"work_order_id": work_order_id, "reason": str(action.args.get("reason") or "user finished at milestone boundary")},
        )
        return f"Minion work order finished ({result.get('status') or 'unknown'})."

    async def _handle_question_answer_async(self, action: ControlAction) -> str:
        work_order_id = str(action.args.get("work_order_id") or action.target_id or "").strip()
        question_id = str(action.args.get("question_id") or "").strip()
        if not work_order_id or not question_id:
            return "Minion question answer is missing work_order_id or question_id."
        answer = {
            "question_id": question_id,
            "selected_option_id": str(action.args.get("selected_option_id") or "").strip(),
            "answer": str(action.args.get("answer") or "").strip(),
            "run_id": str(action.args.get("run_id") or ""),
            "minion_id": str(action.args.get("minion_id") or ""),
            "turn_index": action.args.get("turn_index", 0),
            "plan_revision": action.args.get("plan_revision", 0),
        }
        result = await _to_thread(self._repository().record_clarification_answer, work_order_id, answer)
        if str(result.get("status") or RuntimeStatus.OK) != RuntimeStatus.OK:
            return str(result.get("error") or "Minion question answer failed.")
        resume = await _to_thread(self._resume_architect_after_question_answer, work_order_id, action)
        if str(resume.get("status") or "") == "resumed":
            run_id = str((resume.get("run") or {}).get("run_id") or "")
            return f"Minion question answer recorded; architect resumed{f' as {run_id}' if run_id else ''}."
        if str(resume.get("status") or "") == "skipped":
            return "Minion question answer recorded."
        return f"Minion question answer recorded, but architect resume failed: {resume.get('error') or resume.get('status') or 'unknown error'}"

    async def _handle_question_interaction_async(self, action: ControlAction) -> Any:
        session = minion_question_session(action.args)
        if action.action_kind == "minion_question_submit":
            if not minion_question_ready(session):
                if action.route is None:
                    return "Minion question needs more answers before submit."
                delivery = minion_question_update_delivery(session, action.route)
                return {"delivery": delivery} if delivery is not None else "Minion question needs more answers before submit."
            return await self._submit_question_clarification(session, action.route)
        if action.action_kind == "minion_question_nav":
            session["current_index"] = action.args.get("target_index", session.get("current_index", 0))
            if action.route is None:
                return "Minion question page updated."
            delivery = minion_question_update_delivery(session, action.route)
            return {"delivery": delivery} if delivery is not None else "Minion question page updated."
        updated = minion_question_session_with_selection(
            session,
            question_id=str(action.args.get("question_id") or ""),
            selected_option_id=str(action.args.get("selected_option_id") or ""),
            answer=str(action.args.get("answer") or ""),
        )
        if action.route is None:
            return "Minion question answer recorded."
        delivery = minion_question_update_delivery(updated, action.route)
        return {"delivery": delivery} if delivery is not None else "Minion question answer recorded."

    async def _submit_question_clarification(self, session: dict[str, Any], route: ControlRoute | None) -> Any:
        clarification = {
            "clarification_id": str(session.get("clarification_id") or ""),
            "run_id": str(session.get("run_id") or ""),
            "minion_id": str(session.get("minion_id") or ""),
            "work_order_id": str(session.get("work_order_id") or ""),
            "turn_index": session.get("turn_index", 0),
            "plan_revision": session.get("plan_revision", 0),
            "answers": minion_question_answers(session),
        }
        self._ensure_manager_started()
        try:
            result = await _to_thread(self.client.send_clarification_sync, clarification)
        except Exception as exc:
            return f"Minion clarification submit failed: {exc}"
        if not bool(result.get("ok", True)):
            return str(result.get("error") or "Minion clarification submit failed.")
        if route is None:
            return "Architect input received; continuing planning."
        delivery = minion_question_resolve_delivery(session, route, "Architect input received. Continuing planning.")
        return {"delivery": delivery} if delivery is not None else "Architect input received; continuing planning."

    async def _handle_lesson_decision_async(self, action: ControlAction) -> str:
        decision = str(action.args.get("decision") or "").strip().lower()
        work_order_id = str(action.args.get("work_order_id") or action.target_id or "").strip()
        task_lessons = _string_list(action.args.get("task_lessons"))
        system_lessons = _string_list(action.args.get("system_lessons"))
        memory_candidates = _dict_list(action.args.get("memory_candidates"))
        if decision == "reject":
            return "Minion lessons discarded."
        if decision == "edit":
            return "Lesson absorption paused. Reply with the edited lesson text and Pal can save the revised version."
        if decision != "accept":
            return "Unsupported minion lesson decision."
        result = await _to_thread(
            self._repository().absorb_lessons,
            work_order_id,
            task_lessons=task_lessons,
            system_lessons=system_lessons,
            minion_id=str(action.args.get("minion_id") or ""),
            run_id=str(action.args.get("run_id") or ""),
        )
        if str(result.get("status") or RuntimeStatus.OK) != RuntimeStatus.OK:
            return str(result.get("error") or "Minion lesson absorption failed.")
        l3_result = await _to_thread(
            self._commit_approved_memory_to_l3,
            work_order_id,
            task_lessons,
            system_lessons,
            memory_candidates,
        )
        total = int(result.get("task_lesson_count") or 0) + int(result.get("system_lesson_count") or 0)
        reviewed = total + len(memory_candidates)
        if l3_result:
            return f"Minion memory accepted ({reviewed} reviewed; {total} lessons stored; {l3_result})."
        return f"Minion memory accepted ({reviewed} reviewed; {total} lessons stored)."

    def _commit_lessons_to_l3(self, work_order_id: str, task_lessons: list[str], system_lessons: list[str]) -> str:
        return self._commit_approved_memory_to_l3(work_order_id, task_lessons, system_lessons, [])

    def _commit_approved_memory_to_l3(
        self,
        work_order_id: str,
        task_lessons: list[str],
        system_lessons: list[str],
        memory_candidates: list[dict[str, Any]],
    ) -> str:
        if self.context is None:
            return ""
        runtime = getattr(self.context, "execution_runtime", None)
        if runtime is None or "op_memory_write" not in getattr(runtime, "capabilities", {}):
            return ""
        committed = 0
        for lesson in task_lessons:
            if self._commit_one_lesson_to_l3(runtime, lesson, scope="task", work_order_id=work_order_id):
                committed += 1
        for lesson in system_lessons:
            if self._commit_one_lesson_to_l3(runtime, lesson, scope="system", work_order_id=work_order_id):
                committed += 1
        for candidate in memory_candidates:
            if self._commit_one_memory_candidate_to_l3(runtime, candidate, work_order_id=work_order_id):
                committed += 1
        return f"{committed} memory records committed" if committed else ""

    def _commit_one_lesson_to_l3(self, runtime: Any, lesson: str, *, scope: str, work_order_id: str) -> bool:
        text = str(lesson or "").strip()
        if not text:
            return False
        title = f"Minion {scope} lesson: {_preview_text(text, limit=72)}"
        result = runtime.execute(
            CapabilityCall(
                name="op_memory_write",
                args={
                    "kind": "case",
                    "title": title,
                    "summary": text,
                    "search_text": text,
                    "scope": scope,
                    "task_id": work_order_id if scope == "task" else "",
                    "topics": ["minion", "lesson"],
                    "payload": {"source": "minion", "work_order_id": work_order_id},
                },
            )
        )
        return str(getattr(result, "status", "") or "") == RuntimeStatus.OK

    def _commit_one_memory_candidate_to_l3(self, runtime: Any, candidate: dict[str, Any], *, work_order_id: str) -> bool:
        args = _l3_commit_args_from_memory_candidate(candidate, work_order_id=work_order_id)
        if not args:
            return False
        result = runtime.execute(CapabilityCall(name="op_memory_write", args=args))
        return str(getattr(result, "status", "") or "") == RuntimeStatus.OK

    def has_pending_events(self) -> bool:
        with self._buffer_lock:
            return bool(self._buffered_events)

    def drain_events_sync(self, *, limit: int = 20) -> dict[str, Any]:
        with self._buffer_lock:
            drained = self._buffered_events[:limit]
            del self._buffered_events[:limit]
            remaining = len(self._buffered_events)
        return {"events": drained, "remaining": remaining}

    def record_minion_observation(self, payload: dict[str, Any]) -> None:
        observation = _minion_observation_from_terminal(payload)
        if not observation:
            return
        with self._buffer_lock:
            key = str(observation.get("run_id") or observation.get("work_order_id") or "")
            self.recent_observations.upsert(key, observation)

    def recent_minion_observations(self, *, limit: int = 5) -> list[dict[str, Any]]:
        with self._buffer_lock:
            return [dict(item) for item in self.recent_observations.values(limit=max(1, min(int(limit or 5), 10)))]

    def _ensure_manager_started(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                self.last_health = self.client.health_sync()
                self._start_event_subscription()
                return
            except Exception:
                self._stop_process_only()
        try:
            self.last_health = self.client.health_sync()
            self.last_error = ""
            self._start_event_subscription()
            return
        except Exception:
            pass
        self._cleanup_stale_endpoint()
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        minion_log_path(self.runtime_root).parent.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "pal.minion.manager_main", "--runtime-root", str(self.runtime_root)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=python_subprocess_env(),
            start_new_session=True,
        )
        for _ in range(150):
            if self.process.poll() is not None:
                self._stop_process_only()
                raise RuntimeError("minion manager exited during startup")
            try:
                self.last_health = self.client.health_sync()
                self.last_error = ""
                self._start_event_subscription()
                return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("minion manager failed to start")

    def _stop_manager(self, *, force: bool = False) -> None:
        self._stop_event_subscription()
        process = self.process
        if process is not None and process.poll() is None:
            if not force and self._has_active_runs_sync():
                self.process = None
                self._join_event_subscription()
                self.last_health = {}
                with self._buffer_lock:
                    self._buffered_events.clear()
                return
            self._stop_active_runs_sync()
            with contextlib.suppress(Exception):
                self.client.shutdown_sync()
            with contextlib.suppress(Exception):
                process.wait(timeout=2.0)
        self._stop_process_only()
        self._join_event_subscription()
        self.last_health = {}
        with self._buffer_lock:
            self._buffered_events.clear()

    def _cleanup_stale_endpoint(self) -> None:
        with contextlib.suppress(Exception):
            self.client.health_sync()
            return
        for path in (self.client.socket_path, minion_port_path(self.runtime_root)):
            if path.exists():
                with contextlib.suppress(FileNotFoundError):
                    path.unlink()

    def _has_active_runs_sync(self) -> bool:
        with contextlib.suppress(Exception):
            result = self.client.list_runs_sync()
            for item in list(result.get("items") or []):
                if not isinstance(item, dict):
                    continue
                if str(item.get("status") or "") in {"starting", "running", "approval_pending", "clarification_pending"}:
                    return True
        return False

    def _stop_process_only(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=1.0)
        except Exception:
            with contextlib.suppress(Exception):
                process.kill()
                process.wait(timeout=1.0)
        self.process = None

    def _stop_active_runs_sync(self) -> None:
        with contextlib.suppress(Exception):
            result = self.client.list_runs_sync()
            for item in list(result.get("items") or []):
                if not isinstance(item, dict):
                    continue
                if str(item.get("status") or "") not in {"starting", "running", "approval_pending", "clarification_pending"}:
                    continue
                run_id = str(item.get("run_id") or "").strip()
                if run_id:
                    with contextlib.suppress(Exception):
                        self.client.kill_sync(run_id, reason="minion manager stopping")

    def _start_event_subscription(self) -> None:
        thread = self._event_subscription_thread
        if thread is not None and thread.is_alive():
            return
        self._event_subscription_stop.clear()
        thread = threading.Thread(
            target=self._run_event_subscription_thread,
            name="pal-minion-event-subscription",
            daemon=True,
        )
        self._event_subscription_thread = thread
        thread.start()

    def _stop_event_subscription(self) -> None:
        self._event_subscription_stop.set()
        self._event_subscription_active = False

    def _join_event_subscription(self) -> None:
        thread = self._event_subscription_thread
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=1.0)
        if thread is not None and not thread.is_alive():
            self._event_subscription_thread = None

    def _run_event_subscription_thread(self) -> None:
        while not self._event_subscription_stop.is_set():
            try:
                asyncio.run(self._event_subscription_loop())
            except Exception as exc:
                if self._event_subscription_stop.is_set():
                    return
                self.last_error = f"event subscription failed: {exc.__class__.__name__}: {exc}"
                time.sleep(0.25)

    async def _event_subscription_loop(self) -> None:
        reader, writer = await open_manager_connection(self.runtime_root)
        request_id = f"sub_{uuid4().hex[:16]}"
        try:
            writer.write(
                pack_sidecar_message(
                    {
                        "type": "request",
                        "id": request_id,
                        "method": "subscribe_events",
                        "params": {},
                    }
                )
            )
            await writer.drain()
            response = await read_sidecar_message(reader)
            if str(response.get("id") or "") != request_id or not bool(response.get("ok")):
                raise RuntimeError("minion event subscription rejected")
            self._event_subscription_active = True
            self.last_error = ""
            while not self._event_subscription_stop.is_set():
                frame = await read_sidecar_message(reader)
                if str(frame.get("type") or "") != "event":
                    continue
                event = frame.get("event")
                if isinstance(event, dict):
                    self._buffer_event(event)
        finally:
            self._event_subscription_active = False
            with contextlib.suppress(Exception):
                writer.write(
                    pack_sidecar_message(
                        {
                            "type": "request",
                            "id": f"unsub_{uuid4().hex[:16]}",
                            "method": "unsubscribe_events",
                            "params": {},
                        }
                    )
                )
                await writer.drain()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def _buffer_event(self, event: dict[str, Any]) -> None:
        if _is_high_cardinality_minion_event(event):
            return
        key = _event_dedupe_key(event)
        with self._buffer_lock:
            if key in self._seen_event_keys:
                return
            self._seen_event_keys.add(key)
            if len(self._seen_event_keys) > 1000:
                self._seen_event_keys = set(list(self._seen_event_keys)[-500:])
            self._buffered_events.append(dict(event))
        notify = self.event_notify
        if callable(notify):
            notify()

    def _request_or_error(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            self._ensure_manager_started()
            result = self.client.request_sync(method, params)
            self.last_health = dict(result)
            return result
        except Exception as exc:
            return {"status": RuntimeStatus.ERROR, "error": f"{exc.__class__.__name__}: {exc}", **self._status_payload()}

    def _repository(self) -> MinionTaskingRepository:
        repository = MinionTaskingRepository(runtime_root=self.runtime_root)
        repository.ensure_schema()
        return repository

    def _active_runs_snapshot(self) -> list[dict[str, Any]]:
        if self.process is None or self.process.poll() is not None:
            return []
        try:
            result = self.client.list_runs_sync()
        except Exception:
            return []
        return [dict(item) for item in list(result.get("items") or []) if isinstance(item, dict)]

    def _status_payload(self) -> dict[str, Any]:
        running = self.process is not None and self.process.poll() is None
        with self._buffer_lock:
            buffered_event_count = len(self._buffered_events)
        config = effective_minion_runtime_config(self.runtime_root)
        payload = {
            "module_id": "minion",
            "mounted": self.mounted,
            "degraded": self.degraded,
            "manager_running": running,
            "log_path": str(minion_log_path(self.runtime_root)),
            "configured_runner_mode": str(config.get("runner_mode") or ""),
            "minion_db_path": str(config.get("db_path") or ""),
            "last_error": self.last_error,
            "buffered_event_count": buffered_event_count,
            "event_subscription_active": bool(self._event_subscription_active),
            "profile_count": len(self._profile_registry().list_profiles()),
        }
        payload.update(dict(self.last_health or {}))
        return payload

    def _profile_registry(self) -> MinionProfileRegistry:
        profile_providers: list[Any] = []
        capability_providers: list[Any] = []
        if self.context is not None:
            for handle in self.context.module_registry.modules.values():
                if not bool(getattr(handle, "mounted", True)) or bool(getattr(handle, "degraded", False)):
                    continue
                for provider in handle.ports.values():
                    if callable(getattr(provider, "declared_minion_profiles", None)):
                        profile_providers.append(provider)
                    if callable(getattr(provider, "capabilities_for_minion_profile", None)):
                        capability_providers.append(provider)
        return MinionProfileRegistry(
            profile_providers=tuple(profile_providers),
            capability_providers=tuple(capability_providers),
            ambient_capabilities=tuple(self._ambient_capabilities()),
            runtime_root=self.runtime_root,
        )

    def _ambient_capabilities(self) -> list[str]:
        if self.context is None:
            return []
        try:
            specs = self.context.execution_runtime.list_capability_specs()
        except Exception:
            return []
        return [str(spec.get("name") or "").strip() for spec in specs if str(spec.get("name") or "").strip()]

    def _inject_control_route(self, pack: TaskContextPack, call: CapabilityCall) -> TaskContextPack:
        metadata = dict(pack.metadata)
        if isinstance(metadata.get("control_route"), dict):
            return pack
        route = _control_route_payload_for_turn(self.context, str(call.meta.get("turn_id") or ""))
        if not route:
            return pack
        metadata["control_route"] = route
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})

    def _inject_debug_log_request(self, pack: TaskContextPack, call: CapabilityCall) -> TaskContextPack:
        metadata = dict(pack.metadata)
        if isinstance(metadata.get("debug_log"), dict):
            return pack
        if not _prompt_log_enabled_for_turn(self.context, str(call.meta.get("turn_id") or "")):
            return pack
        metadata["minion_debug_log_enabled"] = True
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})

    def _inject_preferred_endpoint(self, pack: TaskContextPack, call: CapabilityCall) -> TaskContextPack:
        preferred_endpoint_id = str(call.args.get("preferred_endpoint_id") or "").strip()
        if not preferred_endpoint_id:
            return pack
        metadata = dict(pack.metadata)
        metadata["preferred_endpoint_id"] = preferred_endpoint_id
        metadata["preferred_endpoint_source"] = "user"
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})

    def _resume_architect_after_question_answer(self, work_order_id: str, action: ControlAction) -> dict[str, Any]:
        repository = self._repository()
        try:
            pack = repository.pack_for_work_order(work_order_id)
        except Exception as exc:
            return {"status": "error", "error": str(exc) or exc.__class__.__name__}
        pack = self._profile_registry().resolve_pack(pack)
        if not _is_architect_pack(pack):
            return {"status": "skipped", "reason": "work_order_is_not_architect", "work_order_id": work_order_id}
        metadata = _architect_resume_metadata(pack, action)
        if action.route is not None:
            metadata["control_route"] = _control_route_payload_from_route(action.route)
        resumed = TaskContextPack.from_dict(
            {
                **pack.to_dict(),
                "metadata": metadata,
                "minion_profile": pack.minion_profile,
            }
        )
        resumed = repository.prepare_pack_for_spawn(resumed)
        self._ensure_manager_started()
        result = self.client.spawn_sync(resumed.to_dict())
        return {"status": "resumed", "work_order_id": work_order_id, "run": dict(result or {})}


def inspect_minion(provider: MinionManagerProvider) -> MinionSnapshot:
    payload = provider._status_payload()
    return MinionSnapshot(
        mounted=provider.mounted,
        degraded=provider.degraded,
        manager_running=bool(payload.get("manager_running")),
        active_count=int(payload.get("active_count") or 0),
        run_count=int(payload.get("run_count") or 0),
        pending_event_count=int(payload.get("pending_event_count") or 0),
    )


def _is_architect_pack(pack: TaskContextPack) -> bool:
    metadata = dict(pack.metadata or {})
    execution_contract: dict[str, Any] = {}
    for source in (
        dict(pack.resolved_profile or {}).get("effective_execution_contract"),
        dict(pack.resolved_profile or {}).get("execution_contract"),
        pack.workspace.get("execution_contract") if isinstance(pack.workspace, dict) else {},
        metadata.get("execution_contract"),
    ):
        if isinstance(source, dict):
            execution_contract.update(dict(source))
    role = str(
        execution_contract.get("module_role")
        or execution_contract.get("artifact_role")
        or execution_contract.get("role")
        or ""
    ).strip().lower()
    if role == "architect":
        return True
    planner_work_order = metadata.get("planner_work_order")
    if isinstance(planner_work_order, dict) and str(planner_work_order.get("role") or "").strip().lower() == "architect":
        return True
    if isinstance(metadata.get("architect_work_order"), dict):
        return True
    prompt_view = prompt_view_from_metadata(metadata, workspace=dict(pack.workspace))
    return str(prompt_view.get("role") or "").strip().lower() == "architect"


def _architect_resume_metadata(pack: TaskContextPack, action: ControlAction) -> dict[str, Any]:
    metadata = dict(pack.metadata or {})
    answers = [dict(item) for item in list(metadata.get("clarification_answers") or []) if isinstance(item, dict)]
    if not answers:
        answers = [
            {
                "question_id": str(action.args.get("question_id") or ""),
                "selected_option_id": str(action.args.get("selected_option_id") or ""),
                "answer": str(action.args.get("answer") or ""),
                "run_id": str(action.args.get("run_id") or ""),
                "minion_id": str(action.args.get("minion_id") or ""),
                "turn_index": action.args.get("turn_index", 0),
                "plan_revision": action.args.get("plan_revision", 0),
            }
        ]
        metadata["clarification_answers"] = answers
    planner_work_order = dict(metadata.get("planner_work_order") or {})
    if not planner_work_order:
        planner_work_order = build_planner_work_order(
            goal=pack.goal or pack.instruction,
            task_id=str(metadata.get("task_id") or ""),
            work_order_id=pack.work_order_id,
            turn_index=0,
            plan_revision=0,
        )
    if _is_architect_pack(pack):
        planner_work_order["role"] = "architect"
    planner_work_order["turn_index"] = int(_coerce_int(planner_work_order.get("turn_index"), default=0)) + 1
    planner_work_order["plan_revision"] = int(_coerce_int(planner_work_order.get("plan_revision"), default=0)) + 1
    planner_work_order["clarifications"] = answers
    metadata["planner_work_order"] = planner_work_order
    metadata.pop("prompt_view", None)
    return metadata


def _control_route_payload_from_route(route: Any) -> dict[str, Any]:
    if route is None:
        return {}
    return {
        "endpoint_id": str(getattr(route, "endpoint_id", "") or ""),
        "channel_kind": str(getattr(route, "channel_kind", "") or ""),
        "reply_target": dict(getattr(route, "reply_target", {}) or {}),
        "control_scope_key": str(getattr(route, "control_scope_key", "") or ""),
        "correlation_id": str(getattr(route, "correlation_id", "") or ""),
    }


def _coerce_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _minion_observation_from_terminal(payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status") or "").strip()
    work_order_id = str(payload.get("work_order_id") or "").strip()
    run_id = str(payload.get("run_id") or "").strip()
    if not status or not (work_order_id or run_id):
        return {}
    artifacts = [dict(item) for item in list(payload.get("artifacts") or []) if isinstance(item, dict)]
    primary = payload.get("primary_artifact")
    return {
        "status": status,
        "summary": _preview_text(str(payload.get("summary") or ""), limit=500),
        "run_id": run_id,
        "work_order_id": work_order_id,
        "minion_id": str(payload.get("minion_id") or ""),
        "profile": str(payload.get("minion_profile") or "minion"),
        "completed_at": str(payload.get("created_at") or utc_now()),
        "artifacts": artifacts,
        "primary_artifact": dict(primary) if isinstance(primary, dict) else (artifacts[0] if artifacts else {}),
    }


def register_with_core(context: MainContext, service: object | None = None, *, runtime_root: Path | None = None) -> ModuleHandle:
    _ = service
    resolved_root = Path(runtime_root or context.execution_runtime.runtime_root or Path.cwd() / ".pal-minion")
    provider = MinionManagerProvider(runtime_root=resolved_root, context=context)
    provider.event_notify = lambda: getattr(context.port_registry.get("core:core"), "notify_ready", lambda: None)()
    event_provider = _LegacyTaskingEventProvider(service) if hasattr(service, "minion_mailbox") else provider
    source = MinionEventSource(provider=event_provider)
    event_handler = MinionControlEventHandler(provider=provider)
    prompt_provider = TaskingPromptFragmentProvider(manager=provider)
    handle = ModuleHandle(
        module_id="minion",
        tier=MODULE_TIER_DETACHABLE,
        detachable=True,
        introspection_provider=provider,
        prompt_fragment_providers=[prompt_provider],
        supports_lifecycle_capabilities=True,
        event_sources=[source],
        event_handlers={
            EventKind.APPROVAL_REQUEST: [event_handler],
            EventKind.MINION_PROGRESS: [event_handler],
            EventKind.MINION_CHECKPOINT: [event_handler],
            EventKind.MINION_TERMINAL: [event_handler],
            EventKind.MINION_MODULE_COMPLETED: [event_handler],
            EventKind.MINION_WORK_ORDER_COMPLETED: [event_handler],
            EventKind.MINION_CLARIFICATION_REQUEST: [event_handler],
        },
        control_action_handlers={
            "minion_approval_decision": provider.handle_control_action_async,
            "minion_lesson_decision": provider.handle_control_action_async,
            "minion_question_select": provider.handle_control_action_async,
            "minion_question_nav": provider.handle_control_action_async,
            "minion_question_submit": provider.handle_control_action_async,
            "minion_question_answer": provider.handle_control_action_async,
            "minion_plan_accept_override": provider.handle_control_action_async,
            "minion_plan_reject": provider.handle_control_action_async,
            "minion_plan_edit": provider.handle_control_action_async,
            "minion_dag_tick": provider.handle_control_action_async,
            "minion_plan_pause": provider.handle_control_action_async,
            "minion_plan_finish": provider.handle_control_action_async,
        },
        ports={"minion": provider},
        shutdown_sync=provider._stop_manager,
    )
    context.register_module(handle)
    context.event_source_registry.attach("minion", source)
    context.event_handler_registry.register(EventKind.APPROVAL_REQUEST, event_handler, module_id="minion")
    context.event_handler_registry.register(EventKind.MINION_PROGRESS, event_handler, module_id="minion")
    context.event_handler_registry.register(EventKind.MINION_CHECKPOINT, event_handler, module_id="minion")
    context.event_handler_registry.register(EventKind.MINION_TERMINAL, event_handler, module_id="minion")
    context.event_handler_registry.register(EventKind.MINION_MODULE_COMPLETED, event_handler, module_id="minion")
    context.event_handler_registry.register(EventKind.MINION_WORK_ORDER_COMPLETED, event_handler, module_id="minion")
    context.event_handler_registry.register(EventKind.MINION_CLARIFICATION_REQUEST, event_handler, module_id="minion")
    context.prompt_fragment_registry.register(prompt_provider)
    return handle


TaskingIntrospectionProvider = MinionManagerProvider
TaskingSnapshot = MinionSnapshot


def inspect_tasking(provider: MinionManagerProvider) -> MinionSnapshot:
    return inspect_minion(provider)


def _control_route_payload_for_turn(context: MainContext | None, turn_id: str) -> dict[str, Any]:
    if context is None or not str(turn_id or "").strip():
        return {}
    core = context.port_registry.get("core:core")
    continuation = getattr(getattr(core, "state", None), "active_turns", {}).get(str(turn_id))
    channel_envelope = getattr(continuation, "channel_envelope", None)
    if channel_envelope is None:
        return {}
    try:
        from pal.control.routing import route_from_channel_envelope

        route = route_from_channel_envelope(channel_envelope)
    except Exception:
        return {}
    return {
        "endpoint_id": route.endpoint_id,
        "channel_kind": route.channel_kind,
        "reply_target": dict(route.reply_target),
        "control_scope_key": route.control_scope_key,
        "correlation_id": route.correlation_id,
    }


def _prompt_log_enabled_for_turn(context: MainContext | None, turn_id: str) -> bool:
    if context is None:
        return False
    core = context.port_registry.get("core:core")
    state = getattr(core, "state", None)
    if state is None:
        return False
    if str(turn_id or "").strip():
        continuation = getattr(state, "active_turns", {}).get(str(turn_id))
        snapshot = dict(getattr(continuation, "turn_settings_snapshot", {}) or {})
        if "prompt_log_enabled" in snapshot:
            return bool(snapshot.get("prompt_log_enabled"))
    return bool(getattr(state, "prompt_log_enabled", False))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = " ".join(str(item or "").split())
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _l3_commit_args_from_memory_candidate(candidate: dict[str, Any], *, work_order_id: str) -> dict[str, Any]:
    return l3_commit_args_from_memory_candidate(
        candidate,
        default_scope="task",
        fallback_task_id=work_order_id,
    )


def _preview_text(text: str, *, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _event_dedupe_key(event: dict[str, Any]) -> str:
    try:
        return json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return repr(sorted(dict(event or {}).items()))


def _is_high_cardinality_minion_event(event: dict[str, Any]) -> bool:
    return str((event or {}).get("event_kind") or "").strip() in {"phase_started", "progress"}


@dataclass
class _LegacyTaskingEventProvider:
    service: object

    def has_pending_events(self) -> bool:
        mailbox = getattr(self.service, "minion_mailbox", None)
        return bool(getattr(mailbox, "has_pending", lambda: False)())

    def drain_events_sync(self, *, limit: int = 20) -> dict[str, Any]:
        mailbox = getattr(self.service, "minion_mailbox", None)
        if mailbox is None:
            return {"events": [], "remaining": 0}
        drained = list(mailbox.drain())[:limit]
        events = []
        for item in drained:
            payload = getattr(item, "payload", None)
            event_name = str(getattr(item, "event_name", "") or "")
            events.append(
                {
                    "event_kind": _legacy_event_kind(event_name),
                    "minion_id": getattr(payload, "minion_id", ""),
                    "run_id": getattr(payload, "run_id", ""),
                    "work_order_id": getattr(payload, "work_order_id", ""),
                    "payload": {
                        "summary": getattr(payload, "summary", ""),
                        "status": getattr(payload, "status", ""),
                        "milestone_index": getattr(payload, "milestone_index", 0),
                    },
                    "created_at": "",
                }
            )
        return {"events": events, "remaining": 0}


def _legacy_event_kind(event_name: str) -> str:
    if event_name.endswith("terminal"):
        return "terminal"
    if event_name.endswith("checkpoint"):
        return "checkpoint"
    return "progress"


def _introspection_from_rpc(title: str, payload: dict[str, Any]) -> IntrospectionResult:
    status = _runtime_status(payload)
    return IntrospectionResult(
        status=status,
        text=title,
        structured=payload,
        llm_text=render_titled_structured_for_llm(title, payload),
    )


def _capability_from_rpc(text: str, payload: dict[str, Any]) -> CapabilityResult:
    status = _runtime_status(payload)
    return CapabilityResult(
        status=status,
        text=text,
        structured=payload,
        llm_text=render_titled_structured_for_llm(text, payload),
    )


def _runtime_status(payload: dict[str, Any]) -> str:
    status = str(payload.get("status") or "").strip()
    if status in {item.value for item in RuntimeStatus}:
        return status
    return RuntimeStatus.OK


def _profile_summary_payload(profile: dict[str, Any]) -> dict[str, Any]:
    profile_group = str(profile.get("profile_group") or "general").strip() or "general"
    profile_name = str(profile.get("profile_id") or profile.get("profile_name") or "generic").strip() or "generic"
    summary = str(
        profile.get("description_summary")
        or profile.get("summary")
        or profile.get("description")
        or ""
    ).strip()
    if not summary:
        summary = _first_summary_sentence(str(profile.get("identity_fragment") or profile.get("behavior_fragment") or ""))
    return {
        "profile_group": profile_group,
        "profile_name": profile_name,
        "description_summary": summary or str(profile.get("display_name") or profile_name),
    }


def _first_summary_sentence(text: str, *, limit: int = 180) -> str:
    compact = " ".join(str(text or "").strip().split())
    if not compact:
        return ""
    for marker in (". ", "; "):
        if marker in compact:
            compact = compact.split(marker, 1)[0].strip() + "."
            break
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "..."


def _profile_ref_text(value: Any) -> str:
    return str(value or "").strip().replace("/", ".")


def _workflow_args_with_task_family(args: dict[str, Any], *, repository: Any) -> dict[str, Any]:
    normalized = dict(args or {})
    task_id = str(normalized.get("task_id") or "").strip()
    requested_family = _profile_ref_text(normalized.get("profile_family") or normalized.get("family"))
    if not task_id:
        raise MinionWorkOrderValidationError(
            "task_id is required; call intro_minion_task_search first and op_minion_task_create when no matching task exists",
            field="task_id",
        )
    if str(normalized.get("profile_group") or normalized.get("profile_name") or "").strip():
        raise MinionWorkOrderValidationError(
            "dispatch_workflow no longer accepts profile_group/profile_name; bind profile_family on the task",
            field="profile_name",
        )

    snapshot = repository.read_task(task_id)
    if str(snapshot.get("status") or "") == "not_found":
        raise MinionWorkOrderValidationError(
            "task_id was not found; call op_minion_task_create first",
            field="task_id",
        )

    task = dict(snapshot.get("task") or {})
    metadata = dict(task.get("metadata") or {})
    task_family = _profile_ref_text(task.get("profile_family") or metadata.get("profile_family"))
    if not task_family:
        raise MinionWorkOrderValidationError("task.profile_family is required", field="task_id")
    if requested_family and requested_family != task_family:
        raise MinionWorkOrderValidationError(
            f"task_id uses profile_family {task_family}; dispatch requested {requested_family}",
            field="profile_family",
        )
    normalized["profile_family"] = task_family
    normalized["_task_snapshot"] = task
    normalized["_task_metadata"] = metadata
    return normalized


def _workflow_entry_pack_from_args(
    args: dict[str, Any],
    *,
    repository: Any,
    profile_registry: MinionProfileRegistry,
) -> tuple[TaskContextPack, dict[str, Any]]:
    goal = str(args.get("goal") or "").strip()
    if not goal:
        raise MinionWorkOrderValidationError("goal is required", field="goal")
    profile_family = _profile_ref_text(args.get("profile_family") or args.get("family")) or "general"
    producer_profile = dag_producer_profile_for_family(profile_family)
    software_producer = producer_profile == DEFAULT_ARCHITECT_PROFILE
    workspace = _normalize_workflow_workspace(
        dict(args.get("workspace") or {}),
        require_primary_language=profile_family == "software_engineering",
    )
    requirements_review = str(args.get("requirements_review") or "").strip().lower()
    if requirements_review and requirements_review != "skip":
        raise MinionWorkOrderValidationError(
            "requirements_review mode has been removed; Pal must prepare requirements_brief before dispatch_workflow",
            field="requirements_review",
        )
    requested_architecture_mode = str(args.get("architecture_mode") or "auto").strip().lower() or "auto"
    if requested_architecture_mode not in {"auto", "micro", "full"}:
        raise MinionWorkOrderValidationError(
            "architecture_mode must be auto, micro, or full",
            field="architecture_mode",
        )
    architecture_mode = _resolve_architecture_mode(requested_architecture_mode, goal=goal, workspace=workspace) if software_producer else "none"
    interaction_mode = str(args.get("interaction_mode") or "auto_after_plan").strip().lower() or "auto_after_plan"
    if interaction_mode not in {"interactive", "auto_after_plan", "autonomous"}:
        raise MinionWorkOrderValidationError(
            "interaction_mode must be interactive, auto_after_plan, or autonomous",
            field="interaction_mode",
        )
    workflow_id = f"wf_{uuid4().hex[:12]}"
    task_id = str(args.get("task_id") or new_work_id("task")).strip()
    work_order_id = str(args.get("work_order_id") or new_work_id("wo")).strip()
    title = str(args.get("title") or _first_summary_sentence(goal, limit=120) or goal).strip()
    requirements_brief = _manager_requirements_brief_from_args(goal=goal, workspace=workspace, args=args)
    if not software_producer:
        return _generic_dag_workflow_entry_pack(
            args,
            repository=repository,
            profile_registry=profile_registry,
            goal=goal,
            requirements_brief=requirements_brief,
            workspace=workspace,
            profile_family=profile_family,
            requested_architecture_mode=requested_architecture_mode,
            architecture_mode=architecture_mode,
            interaction_mode=interaction_mode,
            task_id=task_id,
            work_order_id=work_order_id,
            title=title,
        )

    initial_profile = producer_profile
    initial_group, initial_name = split_profile_ref(initial_profile)
    workflow = {
        "workflow_id": workflow_id,
        "status": "running",
        "entrypoint": "op_minion_dispatch_workflow",
        "requested_architecture_mode": requested_architecture_mode,
        "architecture_mode": architecture_mode,
        "interaction_mode": interaction_mode,
        "auto_advance_modules": interaction_mode != "interactive",
        "auto_accept_on_review_pass": interaction_mode == "autonomous",
        "profile_family": profile_family,
        "initial_profile": initial_profile,
        "dag_producer": {"kind": "profile", "profile": initial_profile},
        "created_at": utc_now(),
    }
    workflow = append_workflow_step(
        workflow,
        profile=initial_profile,
        input_artifact=requirements_brief,
    )
    approval_policy = dict(args.get("approval_policy") or {}) if isinstance(args.get("approval_policy"), dict) else {}
    metadata: dict[str, Any] = {
        "task_id": task_id,
        "task_title": title,
        "work_order_title": f"Plan: {title}",
        "workflow": workflow,
        "requirements_brief": requirements_brief,
        "architecture_mode": architecture_mode,
        "requested_architecture_mode": requested_architecture_mode,
        "interaction_mode": interaction_mode,
        "profile_family": profile_family,
        "initial_profile": initial_profile,
    }
    planner_work_order = build_planner_work_order(goal=goal, task_id=task_id, work_order_id=work_order_id)
    planner_work_order["role"] = "architect"
    _apply_architecture_mode_requirements(
        planner_work_order,
        architecture_mode=architecture_mode,
        requested_architecture_mode=requested_architecture_mode,
    )
    architect_milestone = {
        "milestone_id": "produce_architecture",
        "title": "Produce reviewed architecture plan",
        "summary": "Inspect the workspace as needed and submit a dispatchable plan draft for plan acceptance review.",
        "acceptance": [
            "Architect submits a canonical dispatchable plan draft.",
            "Plan includes acceptance criteria, module contracts, and test strategy.",
            "Architecture mode constraints are either satisfied or escalated with a concrete reason.",
        ],
    }
    plan_review = {
        "interaction_mode": interaction_mode,
        "auto_accept_on_review_pass": interaction_mode == "autonomous",
        "auto_advance_modules": interaction_mode != "interactive",
    }
    metadata.update(
        {
            "planner_work_order": planner_work_order,
            "architect_work_order": planner_work_order,
            "plan_review": plan_review,
            "milestones": [architect_milestone],
        }
    )
    if approval_policy:
        metadata["approval_policy"] = approval_policy
    pack = TaskContextPack.from_dict(
        {
            "work_order_id": work_order_id,
            "goal": goal,
            "instruction": _architect_workflow_instruction(
                architecture_mode=architecture_mode,
                requested_architecture_mode=requested_architecture_mode,
            ),
            "acceptance_criteria": [
                "Submit a dispatchable plan draft through the plan builder tools.",
                "Include module acceptance criteria and test strategy in the plan.",
                "Do not implement code in the architect run.",
            ],
            "workspace": workspace,
            "profile_group": initial_group or "software_engineering",
            "profile_name": initial_name,
            "metadata": metadata,
        }
    )
    return pack, {
        "workflow_id": workflow_id,
        "requested_architecture_mode": requested_architecture_mode,
        "architecture_mode": architecture_mode,
        "interaction_mode": interaction_mode,
        "initial_profile": initial_profile,
        "profile_family": profile_family,
        "workspace_summary": _workflow_workspace_summary(workspace),
        "next_action": "dag_producer_running",
    }


def _generic_dag_workflow_entry_pack(
    args: dict[str, Any],
    *,
    repository: Any,
    profile_registry: MinionProfileRegistry,
    goal: str,
    requirements_brief: dict[str, Any],
    workspace: dict[str, Any],
    profile_family: str,
    requested_architecture_mode: str,
    architecture_mode: str,
    interaction_mode: str,
    task_id: str,
    work_order_id: str,
    title: str,
) -> tuple[TaskContextPack, dict[str, Any]]:
    workflow_id = f"wf_{uuid4().hex[:12]}"
    task_metadata = dict(args.get("_task_metadata") or {})
    executor = resolve_default_executor_profile(
        profile_family=profile_family,
        registry=profile_registry,
        task_metadata=task_metadata,
        workflow_metadata=dict(args.get("metadata") or {}),
    )
    plan_artifact = build_generic_single_node_plan_artifact(
        goal=goal,
        task_id=task_id,
        profile_family=profile_family,
        requirements_brief=requirements_brief,
        workspace=workspace,
        executor_profile=executor.executor_profile,
        title=title,
    )
    workflow = {
        "workflow_id": workflow_id,
        "status": "running",
        "entrypoint": "op_minion_dispatch_workflow",
        "requested_architecture_mode": requested_architecture_mode,
        "architecture_mode": architecture_mode,
        "interaction_mode": interaction_mode,
        "auto_advance_modules": interaction_mode != "interactive",
        "auto_accept_on_review_pass": interaction_mode == "autonomous",
        "profile_family": profile_family,
        "dag_producer": {
            "kind": "generic_single_node",
            "executor_profile": executor.executor_profile,
            "executor_source": executor.source,
        },
        "created_at": utc_now(),
    }
    workflow = append_workflow_step(
        workflow,
        profile="dag_producer.generic",
        input_artifact=requirements_brief,
        adapter="mechanical_single_node_plan",
    )
    workflow = append_workflow_step(
        workflow,
        profile=executor.executor_profile,
        input_artifact={"plan_id": plan_artifact["plan_id"], "module_id": "main"},
        adapter="dag_node_executor",
    )
    approval_policy = dict(args.get("approval_policy") or {}) if isinstance(args.get("approval_policy"), dict) else {}
    metadata: dict[str, Any] = {
        "task_id": task_id,
        "task_title": title,
        "work_order_title": f"Execute: {title}",
        "workflow": workflow,
        "requirements_brief": requirements_brief,
        "architecture_mode": architecture_mode,
        "requested_architecture_mode": requested_architecture_mode,
        "interaction_mode": interaction_mode,
        "profile_family": profile_family,
        "dag_producer": {
            "kind": "generic_single_node",
            "executor_profile": executor.executor_profile,
            "executor_source": executor.source,
        },
        "plan_execution": {
            "auto_advance_modules": interaction_mode != "interactive",
            "dag_execution": {
                "default_executor_profile": executor.executor_profile,
                "node_executors": {"main": executor.executor_profile},
            },
        },
    }
    if approval_policy:
        metadata["approval_policy"] = approval_policy
    pack = repository.build_plan_parent_pack_from_plan(
        plan_artifact,
        work_order_id=work_order_id,
        workspace=workspace,
        metadata=metadata,
        goal=goal,
        instruction="Execute the generated single-node DAG through the manager DAG consumer.",
    )
    return pack, {
        "workflow_id": workflow_id,
        "requested_architecture_mode": requested_architecture_mode,
        "architecture_mode": architecture_mode,
        "interaction_mode": interaction_mode,
        "initial_profile": executor.executor_profile,
        "profile_family": profile_family,
        "workspace_summary": _workflow_workspace_summary(workspace),
        "next_action": "dag_parent_running",
        "dag_producer": "generic_single_node",
    }


def _normalize_workflow_workspace(workspace: dict[str, Any], *, require_primary_language: bool = True) -> dict[str, Any]:
    normalized = dict(workspace or {})
    kind = str(normalized.get("kind") or normalized.get("workspace_kind") or normalized.get("type") or "").strip().lower()
    cwd = str(normalized.get("cwd") or normalized.get("working_dir") or normalized.get("working_directory") or "").strip()
    repo_path = str(normalized.get("repo_path") or "").strip()
    if not repo_path and cwd and kind in {"existing_repo", "local_repo", "repo", "git_repo", "repository", ""}:
        repo_path = cwd
    if not kind:
        kind = "existing_repo" if repo_path else "new_project"
    if kind in {"local_repo", "repo", "git_repo", "repository"}:
        kind = "existing_repo"
    if kind not in {"new_project", "existing_repo"}:
        raise MinionWorkOrderValidationError("workspace.kind must be new_project or existing_repo", field="workspace.kind")
    normalized["kind"] = kind
    normalized["workspace_kind"] = kind
    if repo_path:
        path = Path(repo_path).expanduser()
        normalized["repo_path"] = str(path)
        normalized.setdefault("source_repo", str(path))
        normalized.setdefault("origin_repo_path", str(path))
        normalized.setdefault("project_name", path.name)
    primary_language = str(normalized.get("primary_language") or "").strip().lower()
    languages = _workflow_language_list(normalized.get("languages"))
    if primary_language and primary_language not in languages:
        languages.insert(0, primary_language)
    if kind == "new_project" and require_primary_language and not primary_language:
        raise MinionWorkOrderValidationError(
            "workspace.primary_language is required for new_project workflows",
            field="workspace.primary_language",
        )
    if primary_language:
        normalized["primary_language"] = primary_language
    if languages:
        normalized["languages"] = languages
    normalized.setdefault("workspace_allocation", {"mode": "runtime_minion_repo"})
    return normalized


def _workflow_language_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = str(item or "").strip().lower()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _resolve_architecture_mode(requested: str, *, goal: str, workspace: dict[str, Any]) -> str:
    if requested in {"micro", "full"}:
        return requested
    if str(workspace.get("kind") or "").strip().lower() == "new_project":
        return "full"
    touched = workspace.get("touched_files") or workspace.get("target_files") or workspace.get("files")
    if isinstance(touched, (list, tuple, set)) and len(touched) > 3:
        return "full"
    text = f"{goal} {workspace.get('scope') or ''} {workspace.get('project_name') or ''}".lower()
    full_markers = (
        "0-1",
        "from scratch",
        "new project",
        "architecture",
        "framework",
        "engine",
        "multi-module",
        "multiple modules",
        "2-3 module",
        "2-3 modules",
        "several modules",
        "parallel",
        "orchestration",
        "system",
        "rewrite",
    )
    return "full" if any(marker in text for marker in full_markers) else "micro"


def _apply_architecture_mode_requirements(
    planner_work_order: dict[str, Any],
    *,
    architecture_mode: str,
    requested_architecture_mode: str,
) -> None:
    requirements = dict(planner_work_order.get("planning_requirements") or {})
    requirements["requested_architecture_mode"] = requested_architecture_mode
    requirements["architecture_mode"] = architecture_mode
    requirements["coder_requires_accepted_plan"] = True
    requirements["module_slug_policy"] = "Use short module ids such as implementation, parser, runtime, tests, final_verification; do not add a module_ prefix."
    if architecture_mode == "micro":
        requirements["micro_plan"] = True
        requirements["preferred_implementation_modules"] = 1
        requirements["max_implementation_modules"] = 2
        requirements["prelude_policy"] = (
            "For narrow micro changes, do not create a prelude/setup_contracts/contracts/baseline/setup module. A no-prelude topology "
            "is valid. Use a prelude only when it must produce real shared importable contracts, stubs, DTOs, facades, generated files, "
            "or setup artifacts before multiple independent implementation modules can start."
        )
        requirements["escalation_allowed"] = True
        requirements["escalation_contract"] = (
            "If the task cannot be planned safely as a small architecture plan, submit an architect escalation instead of weakening "
            "contracts; manager will rerun full planning."
        )
    else:
        requirements["micro_plan"] = False
    planner_work_order["planning_requirements"] = requirements
    planner_work_order["architecture_mode"] = architecture_mode
    planner_work_order["requested_architecture_mode"] = requested_architecture_mode


def _architect_workflow_instruction(*, architecture_mode: str, requested_architecture_mode: str) -> str:
    return (
        f"Produce a {architecture_mode} canonical architecture plan for this workflow. "
        f"The requested architecture_mode is {requested_architecture_mode}. "
        "Use architect plan builder tools only; do not implement code. The plan must include acceptance criteria, module contracts, "
        "test strategy, and dispatchable fork_join_linear topology. In micro mode for narrow changes, use no prelude: produce one "
        "implementation module plus final_verification. Do not create baseline/setup/contracts/prelude modules unless a real shared "
        "code or config artifact must be produced before multiple independent modules can start. If micro scope is unsafe, report escalation_required "
        "with a concrete reason instead of dispatching an under-specified plan."
    )


def _manager_requirements_brief_from_args(*, goal: str, workspace: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    supplied = args.get("requirements_brief")
    if isinstance(supplied, dict):
        brief = dict(supplied)
        brief.setdefault("source", "pal_main_agent")
        brief.setdefault("goal", str(goal or "").strip())
        brief.setdefault("workspace", _workflow_workspace_summary(workspace))
        return brief
    if isinstance(supplied, str) and supplied.strip():
        return {
            "source": "pal_main_agent",
            "goal": str(goal or "").strip(),
            "summary": supplied.strip(),
            "workspace": _workflow_workspace_summary(workspace),
        }
    return _manager_requirements_brief(goal=goal, workspace=workspace)


def _manager_requirements_brief(*, goal: str, workspace: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": "manager_generated",
        "goal": str(goal or "").strip(),
        "workspace": _workflow_workspace_summary(workspace),
        "notes": [
            "Pal main agent did not supply a separate requirements_brief; treat the user goal and workspace facts as the brief.",
            "Ask only for user-owned blockers that cannot be resolved from repository inspection.",
        ],
    }


def _workflow_workspace_summary(workspace: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": str(workspace.get("kind") or ""),
        "project_name": str(workspace.get("project_name") or ""),
        "repo_path": str(workspace.get("repo_path") or ""),
        "origin_repo_path": str(workspace.get("origin_repo_path") or ""),
        "primary_language": str(workspace.get("primary_language") or ""),
        "languages": list(workspace.get("languages") or []),
        "workspace_allocation": dict(workspace.get("workspace_allocation") or {}),
    }


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "accepted"}
    return bool(value)


def _capability_invalid(text: str, exc: MinionWorkOrderValidationError) -> CapabilityResult:
    payload = {"error": str(exc), "error_type": exc.__class__.__name__}
    extra = getattr(exc, "payload", None)
    if isinstance(extra, dict):
        payload.update(extra)
    return CapabilityResult(
        status=RuntimeStatus.INVALID,
        text=text,
        structured=payload,
        llm_text=render_titled_structured_for_llm(text, payload),
    )


def _capability_error(text: str, exc: Exception) -> CapabilityResult:
    payload = {"error": str(exc), "error_type": exc.__class__.__name__}
    extra = getattr(exc, "payload", None)
    if isinstance(extra, dict):
        payload.update(extra)
    return CapabilityResult(
        status=RuntimeStatus.ERROR,
        text=text,
        structured=payload,
        llm_text=render_titled_structured_for_llm(text, payload),
    )


async def _to_thread(fn, *args, **kwargs):
    return await asyncio.to_thread(fn, *args, **kwargs)

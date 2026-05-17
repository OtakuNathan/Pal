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
from pal.control.interactions import (
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
from pal.minion.ipc import MinionManagerClient, minion_log_path, open_manager_connection, python_subprocess_env
from pal.minion.profiles import MinionProfileRegistry
from pal.minion.prompt import TaskingPromptFragmentProvider
from pal.minion.repository import MinionTaskingRepository
from pal.minion.source import MinionControlEventHandler, MinionEventSource
from pal.minion.work_order import build_planner_work_order, prompt_view_from_metadata
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
        "Before spawning, inspect registered profiles with intro_minion_profile_list/read unless the user already named a valid profile; "
        "use only registered canonical_profile_id values and do not invent role names. "
        "For brainstorming or early module-boundary discussion, capture a work-order draft first and let a planner review it; "
        "do not let planner invent boundaries from chat history. Do not use Minion for casual chat, simple Q&A, one-call capabilities, "
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
        "op_minion_draft_work_order",
        "op_minion_promote_work_order_draft",
        "intro_minion_work_order_draft_search",
        "intro_minion_work_order_draft_read",
        "intro_minion_profile_list",
        "intro_minion_profile_read",
        "op_minion_spawn",
        "intro_minion_list",
        "intro_minion_read",
        "intro_minion_work_order_search",
        "intro_minion_work_order_read",
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
        "and intro_minion_work_order_search/read; do not infer progress or current worker from chat. If the user says to replace it, "
        "resolve the active run and work order, kill the old run, then respawn from the work-order continuity. "
        "A task may have only one active work order. Finalize only after reading the work-order fact snapshot."
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
        "op_minion_spawn",
        "op_minion_finalize",
    ),
    priority=95,
    activation_threshold=0.3,
    metadata={"route_family": "minion", "resident": False},
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
        profiles = [profile.to_dict() for profile in self._profile_registry().list_profiles()]
        payload = {
            "items": profiles,
            "count": len(profiles),
            "runtime_profile_dir": str(Path(self.runtime_root) / "plugins" / "minion" / "profiles"),
            "runtime_profile_pattern": "runtime_root/plugins/minion/profiles/**/*.toml",
            "profile_source_order": ["builtin package TOML", "runtime TOML", "mounted provider declarations"],
            "usage": "Use registered canonical_profile_id values as op_minion_spawn.minion_profile; do not invent profile ids.",
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
        args_schema={"type": "object", "properties": {"profile_id": {"type": "string"}}, "required": ["profile_id"]},
    )
    def read_profile(self, call: IntrospectionCall) -> IntrospectionResult:
        profile_id = str(call.args.get("profile_id") or "").strip()
        profile = self._profile_registry().get(profile_id)
        if profile is None:
            payload = {"profile_id": profile_id, "error": "unknown minion profile"}
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
        self._stop_manager()
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
        action_name="spawn",
        description=(
            "Spawn, dispatch, launch, or run a minion worker from a stored work_order_id, reviewed draft_id, "
            "or compiled TaskContextPack. Do not spawn from a bare goal/profile handoff. For structured minion work, "
            "Pal should compile a role-specific prompt_view so the worker sees only its scoped module/milestone. "
            "For repo work, workspace should include repo_path; cwd with type=local_repo is accepted and normalized but repo_path is preferred. "
            "If the user explicitly asks for a specific LLM model or endpoint for this minion, resolve it to an enabled llm endpoint_id "
            "and pass preferred_endpoint_id. If the user does not specify one, omit preferred_endpoint_id so the minion uses the current "
            "Pal active endpoint at request time. "
            "Before choosing minion_profile, call intro_minion_profile_list and intro_minion_profile_read unless the user already named "
            "a valid registered profile. Runtime profile TOML files are loaded from runtime_root/plugins/minion/profiles/*.toml; profile truth is "
            "the live registry, not DB or chat memory. Do not invent profile ids."
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
                "task_context_pack": {
                    "type": "object",
                    "description": (
                        "Compiled TaskContextPack for direct spawn. Prefer metadata.prompt_view or a stored work_order_id; "
                        "do not use a minimal goal-only packet. For repo work, set workspace.repo_path; cwd plus type=local_repo is only a fallback."
                    ),
                    "properties": {
                        "work_order_id": {"type": "string"},
                        "goal": {"type": "string"},
                        "instruction": {"type": "string"},
                        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                        "workspace": {
                            "type": "object",
                            "description": "Workspace facts. For local repo tasks prefer repo_path. If only cwd is known, include type=local_repo so it can be normalized.",
                        },
                        "artifacts": {"type": "array"},
                        "minion_profile": {"type": "string"},
                        "metadata": {
                            "type": "object",
                            "description": (
                                "Work-order metadata. Structured runs should carry prompt_view or a role-specific work order object."
                            ),
                            "properties": {
                                "task_id": {"type": "string"},
                                "task_title": {"type": "string"},
                                "prompt_view": {"type": "object"},
                                "planner_work_order": {"type": "object"},
                                "coder_work_order": {"type": "object"},
                                "reviewer_work_order": {"type": "object"},
                                "milestones": {"type": "array"},
                                "preferred_endpoint_id": {"type": "string"},
                            },
                        },
                    },
                    "required": [
                        "work_order_id",
                        "goal",
                        "instruction",
                        "acceptance_criteria",
                        "workspace",
                        "artifacts",
                        "minion_profile",
                        "metadata",
                    ],
                },
                "task_json": {
                    "type": "string",
                    "description": "Serialized complete TaskContextPack. Do not use it to hide a minimal goal-only packet.",
                },
                "minion_profile": {
                    "type": "string",
                    "description": "Registered minion canonical_profile_id, such as software_engineering.planner. Discover with intro_minion_profile_list/read before use.",
                },
                "preferred_endpoint_id": {
                    "type": "string",
                    "description": (
                        "Optional enabled LLM endpoint_id to use for this minion. Fill only when the user explicitly names a model "
                        "or endpoint for the minion; otherwise omit it so the runner follows Pal's active endpoint setting."
                    ),
                },
                "draft_id": {"type": "string", "description": "Reviewed work-order draft to promote and spawn."},
                "work_order_id": {"type": "string", "description": "Existing stored work order to hydrate and spawn."},
                "final_plan_artifact": {
                    "type": "object",
                    "description": (
                        "Validated planner FinalPlanArtifact. Pair with module_id to spawn one serial coder module work order; "
                        "the manager will advance that module through its milestones one runner turn at a time."
                    ),
                },
                "module_id": {"type": "string", "description": "Module id from final_plan_artifact to run with coder."},
                "goal": {
                    "type": "string",
                    "description": "Optional override when paired with a stored work_order_id; never sufficient by itself.",
                },
                "task_query": {"type": "string", "description": "Resolve exactly one stored work order by search before spawn."},
            },
        },
        metadata={
            "profile_registry_capabilities": ("intro_minion_profile_list", "intro_minion_profile_read"),
            "runtime_profile_dir_template": "runtime_root/plugins/minion/profiles/**/*.toml",
        },
    )
    def spawn(self, call: CapabilityCall) -> CapabilityResult:
        try:
            repository = self._repository()
            pack = _pack_from_args(call.args, repository=repository)
            pack = self._inject_control_route(pack, call)
            pack = self._inject_debug_log_request(pack, call)
            pack = self._inject_preferred_endpoint(pack, call)
            pack = self._profile_registry().resolve_pack(
                pack,
                requested_profile=str(call.args.get("minion_profile") or ""),
            )
            pack = repository.prepare_pack_for_spawn(pack)
            self._ensure_manager_started()
            result = self.client.spawn_sync(pack.to_dict())
            self.last_health = dict(result)
            return _capability_from_rpc("minion spawned", result)
        except Exception as exc:
            return _capability_error("minion spawn failed", exc)

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
                "minion_profile": {
                    "type": "string",
                    "description": "Registered minion canonical_profile_id, such as software_engineering.planner. Discover with intro_minion_profile_list/read before use.",
                },
                "metadata": {"type": "object"},
            },
            "required": [
                "goal",
            ],
        },
    )
    def draft_work_order(self, call: CapabilityCall) -> CapabilityResult:
        try:
            payload = self._repository().create_work_order_draft(dict(call.args))
            return _capability_from_rpc("minion work order draft created", payload)
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
            payload = self._repository().promote_work_order_draft(
                str(call.args.get("draft_id") or ""),
                reviewed_candidate=dict(call.args.get("reviewed_candidate") or {}) or None,
            )
            return _capability_from_rpc("minion work order draft promoted", payload)
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
        action_name="continue_work_order",
        description="Continue a paused or awaiting minion parent work order from its current milestone",
        args_schema={
            "type": "object",
            "properties": {"work_order_id": {"type": "string"}},
            "required": ["work_order_id"],
        },
    )
    def continue_work_order(self, call: CapabilityCall) -> CapabilityResult:
        try:
            self._ensure_manager_started()
            result = self.client.request_sync("continue_work_order", {"work_order_id": str(call.args.get("work_order_id") or "")})
            return _capability_from_rpc("minion work order continued", result)
        except Exception as exc:
            return _capability_error("minion work order continue failed", exc)

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
        if action.action_kind in {"minion_question_select", "minion_question_nav"}:
            return await self._handle_question_interaction_async(action)
        if action.action_kind == "minion_question_answer":
            return await self._handle_question_answer_async(action)
        if action.action_kind in {"minion_plan_continue", "minion_plan_pause", "minion_plan_finish"}:
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
            "edit_note": str(action.args.get("edit_note") or ""),
        }
        self._ensure_manager_started()
        result = await _to_thread(self.client.send_decision_sync, payload)
        status = str(result.get("status") or RuntimeStatus.OK)
        if status == RuntimeStatus.OK:
            if decision == "accept_all":
                return "Minion approval recorded; remaining approvals for this run will be accepted."
            return f"Minion approval {decision or 'decision'} recorded."
        return str(result.get("error") or "Minion approval decision failed.")

    async def _handle_plan_control_async(self, action: ControlAction) -> str:
        work_order_id = str(action.args.get("work_order_id") or action.target_id or "").strip()
        if not work_order_id:
            return "Minion work order action is missing work_order_id."
        self._ensure_manager_started()
        if action.action_kind == "minion_plan_continue":
            result = await _to_thread(self.client.request_sync, "continue_work_order", {"work_order_id": work_order_id})
            if str(result.get("status") or "") == "running_module":
                module_id = str(result.get("module_id") or "")
                return f"Minion work order continued{f' with {module_id}' if module_id else ''}."
            return f"Minion work order continue result: {result.get('status') or 'unknown'}."
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
        resume = await _to_thread(self._resume_planner_after_question_answer, work_order_id, action)
        if str(resume.get("status") or "") == "resumed":
            run_id = str((resume.get("run") or {}).get("run_id") or "")
            return f"Minion question answer recorded; planner resumed{f' as {run_id}' if run_id else ''}."
        if str(resume.get("status") or "") == "skipped":
            return "Minion question answer recorded."
        return f"Minion question answer recorded, but planner resume failed: {resume.get('error') or resume.get('status') or 'unknown error'}"

    async def _handle_question_interaction_async(self, action: ControlAction) -> Any:
        session = minion_question_session(action.args)
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
        if not minion_question_ready(updated):
            if action.route is None:
                return "Minion question answer recorded."
            delivery = minion_question_update_delivery(updated, action.route)
            return {"delivery": delivery} if delivery is not None else "Minion question answer recorded."
        clarification = {
            "clarification_id": str(updated.get("clarification_id") or ""),
            "run_id": str(updated.get("run_id") or ""),
            "minion_id": str(updated.get("minion_id") or ""),
            "work_order_id": str(updated.get("work_order_id") or ""),
            "turn_index": updated.get("turn_index", 0),
            "plan_revision": updated.get("plan_revision", 0),
            "answers": minion_question_answers(updated),
        }
        self._ensure_manager_started()
        try:
            result = await _to_thread(self.client.send_clarification_sync, clarification)
        except Exception as exc:
            return f"Minion clarification submit failed: {exc}"
        if not bool(result.get("ok", True)):
            return str(result.get("error") or "Minion clarification submit failed.")
        if action.route is None:
            return "Planner input received; continuing planning."
        delivery = minion_question_resolve_delivery(updated, action.route, "Planner input received. Continuing planning.")
        return {"delivery": delivery} if delivery is not None else "Planner input received; continuing planning."

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
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        minion_log_path(self.runtime_root).parent.mkdir(parents=True, exist_ok=True)
        self.process = subprocess.Popen(
            [sys.executable, "-m", "pal.minion.manager_main", "--runtime-root", str(self.runtime_root)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=python_subprocess_env(),
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

    def _stop_manager(self) -> None:
        self._stop_event_subscription()
        process = self.process
        if process is not None and process.poll() is None:
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
                if str(item.get("status") or "") not in {"starting", "running", "approval_pending"}:
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
        payload = {
            "module_id": "minion",
            "mounted": self.mounted,
            "degraded": self.degraded,
            "manager_running": running,
            "log_path": str(minion_log_path(self.runtime_root)),
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
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})

    def _resume_planner_after_question_answer(self, work_order_id: str, action: ControlAction) -> dict[str, Any]:
        repository = self._repository()
        try:
            pack = repository.pack_for_work_order(work_order_id)
        except Exception as exc:
            return {"status": "error", "error": str(exc) or exc.__class__.__name__}
        if not _is_planner_pack(pack):
            return {"status": "skipped", "reason": "work_order_is_not_planner", "work_order_id": work_order_id}
        metadata = _planner_resume_metadata(pack, action)
        if action.route is not None:
            metadata["control_route"] = _control_route_payload_from_route(action.route)
        resumed = TaskContextPack.from_dict(
            {
                **pack.to_dict(),
                "metadata": metadata,
                "minion_profile": pack.minion_profile or "software_engineering.planner",
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


def _is_planner_pack(pack: TaskContextPack) -> bool:
    profile = str(pack.minion_profile or "").strip().lower()
    if profile.endswith(".planner") or profile == "planner":
        return True
    metadata = dict(pack.metadata or {})
    if isinstance(metadata.get("planner_work_order"), dict):
        return True
    prompt_view = prompt_view_from_metadata(metadata, workspace=dict(pack.workspace))
    return str(prompt_view.get("role") or "").strip().lower() == "planner"


def _planner_resume_metadata(pack: TaskContextPack, action: ControlAction) -> dict[str, Any]:
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
            EventKind.MINION_CLARIFICATION_REQUEST: [event_handler],
        },
        control_action_handlers={
            "minion_approval_decision": provider.handle_control_action_async,
            "minion_lesson_decision": provider.handle_control_action_async,
            "minion_question_select": provider.handle_control_action_async,
            "minion_question_nav": provider.handle_control_action_async,
            "minion_question_answer": provider.handle_control_action_async,
            "minion_plan_continue": provider.handle_control_action_async,
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
    context.event_handler_registry.register(EventKind.MINION_CLARIFICATION_REQUEST, event_handler, module_id="minion")
    context.prompt_fragment_registry.register(prompt_provider)
    return handle


TaskingIntrospectionProvider = MinionManagerProvider
TaskingSnapshot = MinionSnapshot


def inspect_tasking(provider: MinionManagerProvider) -> MinionSnapshot:
    return inspect_minion(provider)


def _pack_from_args(args: dict[str, Any], *, repository: MinionTaskingRepository | None = None) -> TaskContextPack:
    if isinstance(args.get("task_context_pack"), dict):
        return TaskContextPack.from_dict(dict(args.get("task_context_pack") or {}))
    if str(args.get("task_json") or "").strip():
        return TaskContextPack.from_json(str(args.get("task_json") or ""))
    if isinstance(args.get("final_plan_artifact"), dict):
        if repository is None:
            raise ValueError("repository is required to spawn from final_plan_artifact")
        module_id = str(args.get("module_id") or "").strip()
        if module_id:
            return repository.build_coder_module_pack_from_plan(
                dict(args.get("final_plan_artifact") or {}),
                module_id=module_id,
                work_order_id=str(args.get("work_order_id") or ""),
                workspace=dict(args.get("workspace") or {}),
                metadata=dict(args.get("metadata") or {}),
                goal=str(args.get("goal") or ""),
                instruction=str(args.get("instruction") or ""),
                minion_profile=str(args.get("minion_profile") or "software_engineering.coder"),
            )
        return repository.build_plan_parent_pack_from_plan(
            dict(args.get("final_plan_artifact") or {}),
            work_order_id=str(args.get("work_order_id") or ""),
            workspace=dict(args.get("workspace") or {}),
            metadata=dict(args.get("metadata") or {}),
            goal=str(args.get("goal") or ""),
            instruction=str(args.get("instruction") or ""),
        )
    draft_id = str(args.get("draft_id") or "").strip()
    if draft_id and repository is not None:
        promoted = repository.promote_work_order_draft(
            draft_id,
            reviewed_candidate=dict(args.get("reviewed_candidate") or {}) or None,
        )
        return TaskContextPack.from_dict(dict(promoted.get("task_context_pack") or {}))
    work_order_id = str(args.get("work_order_id") or "").strip()
    if work_order_id:
        if repository is not None:
            return repository.pack_for_work_order(work_order_id, overrides=dict(args))
        return TaskContextPack.from_dict(args)
    query = str(args.get("task_query") or args.get("query") or "").strip()
    if query and repository is not None:
        result = repository.search_work_orders(query, limit=5)
        candidates = list(result.get("items") or [])
        if len(candidates) == 1:
            work_order_id = str(candidates[0].get("work_order_id") or "")
            return repository.pack_for_work_order(
                work_order_id,
                overrides={"metadata": {"resolved_from_task_query": query}},
            )
        raise MinionSpawnResolutionError(query=query, candidates=candidates)
    raise ValueError("task_context_pack, task_json, work_order_id, or task_query is required")


class MinionSpawnResolutionError(ValueError):
    def __init__(self, *, query: str, candidates: list[dict[str, Any]]) -> None:
        super().__init__("minion task query did not resolve to exactly one work order")
        self.payload = {"query": query, "candidates": candidates, "candidate_count": len(candidates)}


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
    kind = str(candidate.get("kind") or candidate.get("document_kind") or "case").strip() or "case"
    scope = str(candidate.get("scope") or "task").strip() or "task"
    title = " ".join(str(candidate.get("title") or "").split())
    summary = " ".join(str(candidate.get("summary") or candidate.get("search_text") or title).split())
    search_text = str(candidate.get("search_text") or summary or title).strip()
    if not title:
        title = _preview_text(summary or search_text, limit=72)
    if not summary:
        summary = search_text or title
    if not search_text:
        search_text = summary or title
    if not kind or not title or not summary or not search_text:
        return {}
    topics_payload = candidate.get("topics")
    if isinstance(topics_payload, str):
        topics = [topics_payload]
    elif isinstance(topics_payload, (list, tuple)):
        topics = list(topics_payload)
    else:
        topics = []
    args: dict[str, Any] = {
        "kind": kind,
        "scope": scope,
        "title": title,
        "summary": summary,
        "search_text": search_text,
        "topics": [str(value) for value in topics if str(value or "").strip()],
        "payload": dict(candidate.get("payload") or {}),
    }
    task_id = str(candidate.get("task_id") or "").strip()
    if task_id:
        args["task_id"] = task_id
    elif scope == "task" and work_order_id:
        args["task_id"] = work_order_id
    for key in ("canonical_key", "situation_text", "task_text", "action_text", "result_text"):
        value = str(candidate.get(key) or "").strip()
        if value:
            args[key] = value
    return args


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

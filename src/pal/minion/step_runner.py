from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from pal.foundation import utc_now
from pal.minion.dag_advancer import dag_state_to_runtime_dict as _dag_state_to_runtime_dict
from pal.minion.profiles import MinionProfileRegistry
from pal.minion.utils import dedupe_strings as _dedupe_strings
from pal.minion.utils import string_list as _string_list
from pal.shared import TaskContextPack


_DEFAULT_MAX_PARALLEL_LLM_NODES = 5
_DEFAULT_MAX_PARALLEL_MODULES = _DEFAULT_MAX_PARALLEL_LLM_NODES
_SUPPORTED_SLOT_RESOURCES = {"logical_minion_slot", "llm_node_slot"}


@dataclass(frozen=True)
class LogicalCoderContext:
    run_id: str
    parent_run_id: str
    work_order_id: str
    module_id: str
    slot_id: str
    pack: TaskContextPack


@dataclass(frozen=True)
class LogicalSlotOwner:
    run_id: str
    work_order_id: str


@dataclass
class LocalLogicalSlotBroker:
    manager: Any

    def available_slots(self) -> int | None:
        return self.manager._available_llm_node_slots()

    def request(self, owner: LogicalSlotOwner, *, resource: str, module_id: str = "", reason: str = "") -> dict[str, Any]:
        # DAG resource isolation: a slot is only a global capacity token. It must
        # not carry mutable coder state, workspace handles, or git checkout state.
        # Each logical coder still gets a separate pack/context and its own
        # prepared git environment before execution.
        normalized_resource = str(resource or "llm_node_slot").strip()
        if normalized_resource not in _SUPPORTED_SLOT_RESOURCES:
            return {
                "status": "denied",
                "reason": "unsupported_resource",
                "resource": normalized_resource,
                "run_id": owner.run_id,
                "work_order_id": owner.work_order_id,
            }
        available = self.manager._available_llm_node_slots()
        if available <= 0:
            return {
                "status": "denied",
                "reason": "global_parallel_limit",
                "limit_kind": "llm_node",
                "resource": normalized_resource,
                "run_id": owner.run_id,
                "work_order_id": owner.work_order_id,
                "max_parallel_llm_nodes": int(self.manager.max_parallel_modules or _DEFAULT_MAX_PARALLEL_LLM_NODES),
                "max_parallel_modules": int(self.manager.max_parallel_modules or _DEFAULT_MAX_PARALLEL_MODULES),
                "active_llm_node_count": self.manager._active_llm_node_slot_count_from_ledger(),
                "active_module_count": self.manager._active_module_child_count(),
                "allocated_logical_slots": self.manager._allocated_logical_slot_count(),
                "available_llm_node_slots": 0,
                "available_module_slots": 0,
                "logical_slot_generation": int(getattr(self.manager, "_logical_slot_generation", 0)),
            }
        slot_id = f"slot_{uuid4().hex[:12]}"
        self.manager._logical_slots[slot_id] = {
            "slot_id": slot_id,
            "resource": normalized_resource,
            "run_id": owner.run_id,
            "work_order_id": owner.work_order_id,
            "module_id": str(module_id or "").strip(),
            "reason": str(reason or "").strip(),
            "granted_at": utc_now(),
        }
        return {
            "status": "granted",
            "slot_id": slot_id,
            "resource": normalized_resource,
            "run_id": owner.run_id,
            "work_order_id": owner.work_order_id,
            "module_id": str(module_id or "").strip(),
            "available_llm_node_slots": self.manager._available_llm_node_slots(),
            "available_module_slots": self.manager._available_module_slots(),
            "logical_slot_generation": int(getattr(self.manager, "_logical_slot_generation", 0)),
        }

    async def wait_available(
        self,
        owner: LogicalSlotOwner,
        *,
        resource: str,
        module_id: str = "",
        reason: str = "",
        timeout_seconds: Any = None,
    ) -> dict[str, Any]:
        normalized_resource = str(resource or "llm_node_slot").strip()
        if normalized_resource not in _SUPPORTED_SLOT_RESOURCES:
            return {
                "status": "unsupported",
                "reason": "unsupported_resource",
                "resource": normalized_resource,
                "run_id": owner.run_id,
                "work_order_id": owner.work_order_id,
                "module_id": str(module_id or "").strip(),
            }
        shutdown_event = getattr(self.manager, "_shutdown_event", None)
        if getattr(shutdown_event, "is_set", lambda: False)():
            return {
                "status": "shutdown",
                "resource": normalized_resource,
                "run_id": owner.run_id,
                "work_order_id": owner.work_order_id,
                "module_id": str(module_id or "").strip(),
                "available_llm_node_slots": self.manager._available_llm_node_slots(),
                "available_module_slots": self.manager._available_module_slots(),
                "logical_slot_generation": int(getattr(self.manager, "_logical_slot_generation", 0)),
            }
        if self.manager._available_llm_node_slots() > 0:
            return {
                "status": "available",
                "resource": normalized_resource,
                "run_id": owner.run_id,
                "work_order_id": owner.work_order_id,
                "module_id": str(module_id or "").strip(),
                "available_llm_node_slots": self.manager._available_llm_node_slots(),
                "available_module_slots": self.manager._available_module_slots(),
                "logical_slot_generation": int(getattr(self.manager, "_logical_slot_generation", 0)),
            }
        generation = int(getattr(self.manager, "_logical_slot_generation", 0))
        event = getattr(self.manager, "_logical_slot_event", None)
        if event is None:
            return {
                "status": "unsupported",
                "reason": "slot_wait_event_unavailable",
                "resource": normalized_resource,
                "run_id": owner.run_id,
                "work_order_id": owner.work_order_id,
                "module_id": str(module_id or "").strip(),
            }
        timeout = _optional_timeout_seconds(timeout_seconds)
        try:
            if timeout is None:
                await event.wait()
            else:
                await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {
                "status": "timeout",
                "reason": "logical_slot_wait_timeout",
                "resource": normalized_resource,
                "run_id": owner.run_id,
                "work_order_id": owner.work_order_id,
                "module_id": str(module_id or "").strip(),
                "timeout_seconds": timeout,
                "available_llm_node_slots": self.manager._available_llm_node_slots(),
                "available_module_slots": self.manager._available_module_slots(),
                "logical_slot_generation": int(getattr(self.manager, "_logical_slot_generation", 0)),
                "previous_logical_slot_generation": generation,
            }
        if getattr(shutdown_event, "is_set", lambda: False)():
            return {
                "status": "shutdown",
                "resource": normalized_resource,
                "run_id": owner.run_id,
                "work_order_id": owner.work_order_id,
                "module_id": str(module_id or "").strip(),
                "available_llm_node_slots": self.manager._available_llm_node_slots(),
                "available_module_slots": self.manager._available_module_slots(),
                "logical_slot_generation": int(getattr(self.manager, "_logical_slot_generation", 0)),
                "previous_logical_slot_generation": generation,
            }
        return {
            "status": "notified",
            "resource": normalized_resource,
            "run_id": owner.run_id,
            "work_order_id": owner.work_order_id,
            "module_id": str(module_id or "").strip(),
            "reason": str(reason or "").strip(),
            "available_llm_node_slots": self.manager._available_llm_node_slots(),
            "available_module_slots": self.manager._available_module_slots(),
            "logical_slot_generation": int(getattr(self.manager, "_logical_slot_generation", 0)),
            "previous_logical_slot_generation": generation,
        }

    def release(self, slot_id: str, *, run_id: str = "", reason: str = "") -> dict[str, Any]:
        normalized = str(slot_id or "").strip()
        if not normalized:
            return {"status": "skipped", "reason": "slot_id_required"}
        slot = self.manager._logical_slots.get(normalized)
        if slot is None:
            return {"status": "not_found", "slot_id": normalized}
        if run_id and str(slot.get("run_id") or "") != str(run_id):
            return {"status": "denied", "reason": "slot_owned_by_different_run", "slot_id": normalized}
        removed = self.manager._logical_slots.pop(normalized)
        notify = getattr(self.manager, "_notify_logical_slot_available", None)
        if callable(notify):
            notify(reason=reason or "logical_slot_release")
        return {
            "status": "released",
            "slot_id": normalized,
            "run_id": str(removed.get("run_id") or ""),
            "work_order_id": str(removed.get("work_order_id") or ""),
            "module_id": str(removed.get("module_id") or ""),
            "reason": str(reason or "").strip(),
            "available_llm_node_slots": self.manager._available_llm_node_slots(),
            "available_module_slots": self.manager._available_module_slots(),
            "logical_slot_generation": int(getattr(self.manager, "_logical_slot_generation", 0)),
        }

    def release_for_run(self, run_id: str, *, reason: str = "") -> list[dict[str, Any]]:
        normalized = str(run_id or "").strip()
        if not normalized:
            return []
        slot_ids = [slot_id for slot_id, slot in self.manager._logical_slots.items() if str(slot.get("run_id") or "") == normalized]
        return [self.release(slot_id, run_id=normalized, reason=reason) for slot_id in slot_ids]


@dataclass
class RpcLogicalSlotBroker:
    client: Any
    _owned_slots: dict[str, str] = field(default_factory=dict)
    _known_available_slots: int | None = None

    def available_slots(self) -> int | None:
        return self._known_available_slots

    def request(self, owner: LogicalSlotOwner, *, resource: str, module_id: str = "", reason: str = "") -> dict[str, Any]:
        result = self.client.request_logical_slot_sync(
            run_id=owner.run_id,
            work_order_id=owner.work_order_id,
            resource=resource,
            module_id=module_id,
            reason=reason,
        )
        self._remember_available_slots(result)
        if str(result.get("status") or "") == "granted":
            self._owned_slots[str(result.get("slot_id") or "")] = owner.run_id
        return result

    async def wait_available(
        self,
        owner: LogicalSlotOwner,
        *,
        resource: str,
        module_id: str = "",
        reason: str = "",
        timeout_seconds: Any = None,
    ) -> dict[str, Any]:
        result = await self.client.request(
            "wait_logical_slot",
            {
                "run_id": owner.run_id,
                "work_order_id": owner.work_order_id,
                "resource": resource,
                "module_id": module_id,
                "reason": reason,
                "timeout_seconds": timeout_seconds,
            },
        )
        self._remember_available_slots(result)
        return result

    def release(self, slot_id: str, *, run_id: str = "", reason: str = "") -> dict[str, Any]:
        result = self.client.release_logical_slot_sync(slot_id=slot_id, run_id=run_id, reason=reason)
        self._remember_available_slots(result)
        if str(result.get("status") or "") == "released":
            self._owned_slots.pop(str(slot_id or ""), None)
        return result

    def release_for_run(self, run_id: str, *, reason: str = "") -> list[dict[str, Any]]:
        normalized = str(run_id or "").strip()
        slot_ids = [slot_id for slot_id, owner_run_id in self._owned_slots.items() if owner_run_id == normalized]
        return [self.release(slot_id, run_id=normalized, reason=reason) for slot_id in slot_ids]

    def _remember_available_slots(self, payload: dict[str, Any]) -> None:
        if "available_llm_node_slots" not in payload and "available_module_slots" not in payload:
            return
        try:
            self._known_available_slots = max(0, int(payload.get("available_llm_node_slots", payload.get("available_module_slots")) or 0))
        except (TypeError, ValueError):
            self._known_available_slots = None


@dataclass
class ModuleStepRunner:
    manager: Any
    slot_broker: Any | None = None

    def __post_init__(self) -> None:
        if self.slot_broker is None:
            self.slot_broker = LocalLogicalSlotBroker(self.manager)

    async def tick_parent_dag(self, work_order_id: str, *, reason: str = "") -> dict[str, Any]:
        normalized = str(work_order_id or "").strip()
        if not normalized:
            raise ValueError("work_order_id is required")
        available_slots = self._available_llm_node_slots()
        if available_slots <= 0:
            snapshot = self.manager.tasking_repository.read_work_order(normalized)
            metadata = dict((snapshot.get("work_order") or {}).get("metadata") or {}) if snapshot.get("status") == "ok" else {}
            plan_execution = dict(metadata.get("plan_execution") or {})
            status = str(plan_execution.get("status") or snapshot.get("status") or "not_available")
            active_child_work_order_ids = _active_child_work_order_ids_from_plan_execution(plan_execution)
            ready_module_ids = _string_list(_plan_execution_dag_state(plan_execution).get("ready_modules"))
            waiting_for_slot = bool(ready_module_ids)
            return {
                "status": "waiting_for_slot" if waiting_for_slot else status,
                "work_order_id": normalized,
                "reason": "global_parallel_limit" if waiting_for_slot else ("active_child_running" if active_child_work_order_ids else "no_available_module_slot"),
                "active_child_work_order_id": active_child_work_order_ids[0] if active_child_work_order_ids else "",
                "active_child_work_order_ids": active_child_work_order_ids,
                "ready_module_ids": ready_module_ids,
                "max_parallel_llm_nodes": self._max_parallel_llm_nodes(),
                "max_parallel_modules": self._max_parallel_modules(),
                "active_llm_node_count": self._active_llm_node_count(),
                "active_module_count": self.manager._active_module_child_count(),
                "available_llm_node_slots": 0,
                "available_module_slots": 0,
                "dag_tick": True,
                "tick_reason": str(reason or "").strip(),
            }
        try:
            packs = self.manager.tasking_repository.next_ready_plan_module_packs(normalized, allow_paused=True, limit=available_slots)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"
            blocked = self.manager.tasking_repository.block_plan_parent(
                normalized,
                reason="dag_ready_module_prepare_failed",
                error=error,
            )
            event = {
                "event_kind": "plan_parent_blocked",
                "minion_id": "",
                "run_id": "",
                "work_order_id": normalized,
                "minion_profile": "",
                "payload": {
                    "status": "blocked",
                    "summary": "DAG ready module preparation failed",
                    "reason": "dag_ready_module_prepare_failed",
                    "error": error,
                    "blocked": dict(blocked),
                    "tick_reason": str(reason or "").strip(),
                },
                "created_at": utc_now(),
            }
            self.manager._queue_event_delivery(event)
            self.manager.tasking_repository.record_minion_event(event)
            return {
                "status": "blocked",
                "work_order_id": normalized,
                "reason": "dag_ready_module_prepare_failed",
                "error": error,
                "blocked": dict(blocked),
                "dag_tick": True,
                "tick_reason": str(reason or "").strip(),
            }
        if not packs:
            snapshot = self.manager.tasking_repository.read_work_order(normalized)
            metadata = dict((snapshot.get("work_order") or {}).get("metadata") or {}) if snapshot.get("status") == "ok" else {}
            plan_execution = dict(metadata.get("plan_execution") or {})
            status = str(plan_execution.get("status") or snapshot.get("status") or "not_available")
            active_child_work_order_ids = _active_child_work_order_ids_from_plan_execution(plan_execution)
            return {
                "status": status,
                "work_order_id": normalized,
                "reason": "active_child_running" if status == "running_module" and active_child_work_order_ids else "no_next_module",
                "active_child_work_order_id": active_child_work_order_ids[0] if active_child_work_order_ids else "",
                "active_child_work_order_ids": active_child_work_order_ids,
                "dag_tick": True,
                "tick_reason": str(reason or "").strip(),
            }
        registry = MinionProfileRegistry(runtime_root=self.manager.runtime_root)
        runs: list[dict[str, Any]] = []
        module_ids: list[str] = []
        child_work_order_ids: list[str] = []
        spawn_failures: list[dict[str, Any]] = []
        for pack in packs:
            resolved_pack = registry.resolve_pack(pack)
            module_id = str(resolved_pack.metadata.get("module_id") or resolved_pack.metadata.get("parent_module_id") or "")
            try:
                run = await self.manager.spawn(resolved_pack.to_dict())
            except Exception as exc:
                release = self.manager.tasking_repository.release_running_module_parent(
                    resolved_pack.work_order_id,
                    child_terminal_status="failed",
                    reason=f"manager failed to spawn module {module_id or resolved_pack.work_order_id}: {exc.__class__.__name__}: {exc}",
                )
                spawn_failures.append(
                    {
                        "module_id": module_id,
                        "child_work_order_id": resolved_pack.work_order_id,
                        "error": f"{exc.__class__.__name__}: {exc}",
                        "release": dict(release),
                    }
                )
                continue
            if str(run.get("status") or "") == "waiting_for_slot":
                release = self.manager.tasking_repository.release_running_module_parent(
                    resolved_pack.work_order_id,
                    child_terminal_status="blocked",
                    reason=f"manager deferred module {module_id or resolved_pack.work_order_id}: global LLM node limit reached",
                )
                spawn_failures.append(
                    {
                        "module_id": module_id,
                        "child_work_order_id": resolved_pack.work_order_id,
                        "error": "global LLM node limit reached",
                        "deferred": dict(run),
                        "release": dict(release),
                    }
                )
                continue
            runs.append(dict(run))
            module_ids.append(module_id)
            child_work_order_ids.append(resolved_pack.work_order_id)
        if not runs and spawn_failures:
            return {
                "status": "spawn_failed",
                "work_order_id": normalized,
                "failures": spawn_failures,
                "failure_count": len(spawn_failures),
                "max_parallel_llm_nodes": self._max_parallel_llm_nodes(),
                "max_parallel_modules": self._max_parallel_modules(),
                "active_llm_node_count": self._active_llm_node_count(),
                "active_module_count": self.manager._active_module_child_count(),
                "available_llm_node_slots": self._available_llm_node_slots(),
                "available_module_slots": self.manager._available_module_slots(),
                "dag_tick": True,
                "tick_reason": str(reason or "").strip(),
            }
        return {
            "status": "running_module",
            "work_order_id": normalized,
            "child_work_order_id": child_work_order_ids[0] if child_work_order_ids else "",
            "child_work_order_ids": child_work_order_ids,
            "module_id": module_ids[0] if module_ids else "",
            "module_ids": module_ids,
            "run": runs[0] if runs else {},
            "runs": runs,
            "spawn_failures": spawn_failures,
            "max_parallel_llm_nodes": self._max_parallel_llm_nodes(),
            "max_parallel_modules": self._max_parallel_modules(),
            "active_llm_node_count": self._active_llm_node_count(),
            "active_module_count": self.manager._active_module_child_count(),
            "available_llm_node_slots": self._available_llm_node_slots(),
            "available_module_slots": self.manager._available_module_slots(),
            "dag_tick": True,
            "tick_reason": str(reason or "").strip(),
        }

    async def tick_ready_plan_dags(self, *, preferred_work_order_id: str = "", reason: str = "") -> dict[str, Any]:
        preferred = str(preferred_work_order_id or "").strip()
        parent_ids = self.manager.tasking_repository.ready_plan_parent_work_order_ids()
        if preferred and preferred in parent_ids:
            parent_ids = [preferred, *[item for item in parent_ids if item != preferred]]
        scheduled: list[dict[str, Any]] = []
        for parent_id in parent_ids:
            if self._available_llm_node_slots() <= 0:
                break
            result = await self.tick_parent_dag(parent_id, reason=reason or "tick_ready_plan_dags")
            if str(result.get("status") or "") == "running_module":
                scheduled.append(dict(result))
        return {
            "status": "scheduled" if scheduled else "idle",
            "reason": reason,
            "scheduled": scheduled,
            "scheduled_count": len(scheduled),
            "max_parallel_llm_nodes": self._max_parallel_llm_nodes(),
            "max_parallel_modules": self._max_parallel_modules(),
            "active_llm_node_count": self._active_llm_node_count(),
            "active_module_count": self.manager._active_module_child_count(),
            "available_llm_node_slots": self._available_llm_node_slots(),
            "available_module_slots": self.manager._available_module_slots(),
            "dag_tick": True,
        }

    def request_logical_slot(self, state: Any, *, resource: str, module_id: str = "", reason: str = "") -> dict[str, Any]:
        assert self.slot_broker is not None
        owner = LogicalSlotOwner(run_id=str(state.run_id or ""), work_order_id=str(state.pack.work_order_id or ""))
        return self.slot_broker.request(owner, resource=resource, module_id=module_id, reason=reason)

    async def wait_logical_slot(
        self,
        state: Any,
        *,
        resource: str,
        module_id: str = "",
        reason: str = "",
        timeout_seconds: Any = None,
    ) -> dict[str, Any]:
        assert self.slot_broker is not None
        wait_available = getattr(self.slot_broker, "wait_available", None)
        if not callable(wait_available):
            return {
                "status": "unsupported",
                "reason": "slot_wait_not_supported",
                "resource": str(resource or "logical_minion_slot"),
                "module_id": str(module_id or "").strip(),
            }
        owner = LogicalSlotOwner(run_id=str(state.run_id or ""), work_order_id=str(state.pack.work_order_id or ""))
        value = wait_available(
            owner,
            resource=resource,
            module_id=module_id,
            reason=reason,
            timeout_seconds=timeout_seconds,
        )
        result = await value if inspect.isawaitable(value) else value
        return dict(result or {})

    def release_logical_slot(self, slot_id: str, *, run_id: str = "", reason: str = "") -> dict[str, Any]:
        assert self.slot_broker is not None
        return self.slot_broker.release(slot_id, run_id=run_id, reason=reason)

    def release_logical_slots_for_run(self, run_id: str, *, reason: str = "") -> list[dict[str, Any]]:
        assert self.slot_broker is not None
        return self.slot_broker.release_for_run(run_id, reason=reason)

    def _available_logical_slot_attempts(self) -> int | None:
        assert self.slot_broker is not None
        available = getattr(self.slot_broker, "available_slots", None)
        if not callable(available):
            return None
        value = available()
        if value is None:
            return None
        return max(0, int(value or 0))

    async def run_logical_module_lane(
        self,
        parent_state: Any,
        pack: TaskContextPack,
        *,
        module_id: str,
        task: Callable[[LogicalCoderContext], Awaitable[dict[str, Any]] | dict[str, Any]],
        reason: str = "",
    ) -> dict[str, Any]:
        grant = self.request_logical_slot(
            parent_state,
            resource="logical_minion_slot",
            module_id=module_id,
            reason=reason or "logical_module_lane",
        )
        if str(grant.get("status") or "") != "granted":
            return {"status": "waiting_for_slot", "module_id": str(module_id or ""), "grant": grant}
        return await self._run_logical_module_lane_with_grant(
            parent_state,
            pack,
            module_id=module_id,
            task=task,
            grant=grant,
        )

    async def _run_logical_module_lane_with_grant(
        self,
        parent_state: Any,
        pack: TaskContextPack,
        *,
        module_id: str,
        task: Callable[[LogicalCoderContext], Awaitable[dict[str, Any]] | dict[str, Any]],
        grant: dict[str, Any],
    ) -> dict[str, Any]:
        context = self.build_logical_coder_context(
            parent_state,
            pack,
            module_id=module_id,
            slot_id=str(grant.get("slot_id") or ""),
        )
        self._bind_logical_slot_to_context(context, parent_state=parent_state)
        try:
            value = task(context)
            result = await value if inspect.isawaitable(value) else value
            return {
                "status": "completed",
                "module_id": context.module_id,
                "logical_run_id": context.run_id,
                "slot_id": context.slot_id,
                "result": dict(result or {}),
            }
        except Exception as exc:
            return {
                "status": "failed",
                "module_id": context.module_id,
                "logical_run_id": context.run_id,
                "slot_id": context.slot_id,
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        finally:
            self.release_logical_slot(context.slot_id, run_id=parent_state.run_id, reason="logical_module_lane_done")

    def _bind_logical_slot_to_context(self, context: LogicalCoderContext, *, parent_state: Any) -> None:
        if not context.slot_id or not hasattr(self.manager, "_logical_slots"):
            return
        slot = self.manager._logical_slots.get(context.slot_id)
        if not isinstance(slot, dict):
            return
        parent_work_order_id = str(slot.get("work_order_id") or getattr(parent_state.pack, "work_order_id", "") or "").strip()
        if parent_work_order_id:
            slot.setdefault("parent_work_order_id", parent_work_order_id)
        if context.work_order_id:
            slot["work_order_id"] = context.work_order_id
        if context.module_id:
            slot["module_id"] = context.module_id

    async def run_logical_minion_runner(
        self,
        parent_state: Any,
        pack: TaskContextPack,
        *,
        module_id: str,
        reason: str = "",
    ) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        result = await self.run_logical_module_lane(
            parent_state,
            pack,
            module_id=module_id,
            task=lambda context: self._run_minion_runner_in_context(context, events),
            reason=reason or "logical_minion_runner",
        )
        payload = {**result, "events": list(events), "event_count": len(events)}
        if str(result.get("status") or "") == "completed" and int(dict(result.get("result") or {}).get("returncode") or 0) != 0:
            payload["status"] = "failed"
            payload["reason"] = "logical_minion_nonzero_returncode"
        return payload

    async def run_logical_minion_runner_dag(
        self,
        parent_state: Any,
        module_packs: dict[str, TaskContextPack],
        depends_on: dict[str, list[str]],
        *,
        reason: str = "",
        fail_fast: bool = True,
        slot_wait_timeout_seconds: Any = None,
    ) -> dict[str, Any]:
        events_by_module: dict[str, list[dict[str, Any]]] = {}

        async def task(module_id: str, context: LogicalCoderContext) -> dict[str, Any]:
            events: list[dict[str, Any]] = []
            events_by_module[module_id] = events
            return await self._run_minion_runner_in_context(context, events)

        result = await self.run_logical_module_lane_dag(
            parent_state,
            module_packs,
            depends_on,
            task=task,
            reason=reason or "logical_minion_runner_dag",
            fail_fast=fail_fast,
            slot_wait_timeout_seconds=slot_wait_timeout_seconds,
        )
        payload = self._logical_minion_dag_result_from_returncodes(result)
        payload["events_by_module"] = {module_id: list(events) for module_id, events in events_by_module.items()}
        payload["event_count"] = sum(len(events) for events in events_by_module.values())
        return payload

    async def _run_minion_runner_in_context(self, context: LogicalCoderContext, events: list[dict[str, Any]]) -> dict[str, Any]:
        control_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        logical_state = self._register_logical_runner_state(context, control_queue)

        async def write_event(event: dict[str, Any]) -> None:
            normalized = self._logical_runner_event(context, event)
            if logical_state is not None:
                normalized["minion_id"] = logical_state.minion_id
            events.append(normalized)
            if logical_state is not None:
                self.manager._record_event(logical_state, normalized)
            else:
                self._record_logical_runner_event(normalized)

        async def read_decision(timeout: float | None = None) -> dict[str, Any] | None:
            if timeout is None:
                return await control_queue.get()
            try:
                return await asyncio.wait_for(control_queue.get(), timeout=max(0.0, float(timeout or 0.0)))
            except asyncio.TimeoutError:
                return None

        from pal.minion.runner import MinionRunner

        runner = MinionRunner(
            runtime_root=self.manager.runtime_root,
            pack=context.pack,
            minion_id=logical_state.minion_id if logical_state is not None else f"logical_minion_{uuid4().hex[:10]}",
            run_id=context.run_id,
            write_event=write_event,
            read_decision=read_decision,
        )
        returncode = await runner.run()
        if not any(str(event.get("event_kind") or "") == "terminal" for event in events):
            status = "completed" if int(returncode or 0) == 0 else "failed"
            await write_event(
                {
                    "event_kind": "terminal",
                    "payload": {
                        "status": status,
                        "summary": f"logical minion exited with code {int(returncode or 0)}",
                        "returncode": int(returncode or 0),
                    },
                    "created_at": utc_now(),
                }
            )
        return {"returncode": int(returncode or 0), "event_count": len(events)}

    def _register_logical_runner_state(
        self,
        context: LogicalCoderContext,
        control_queue: asyncio.Queue[dict[str, Any]],
    ) -> Any | None:
        if not hasattr(self.manager, "runs") or not hasattr(self.manager, "_record_event"):
            return None
        try:
            from pal.minion.manager import MinionRunState
        except Exception:
            return None
        state = self.manager.runs.get(context.run_id)
        if state is None:
            state = MinionRunState(
                minion_id=f"logical_minion_{uuid4().hex[:10]}",
                run_id=context.run_id,
                pack=context.pack,
                runner_kind="logical",
                status="running",
                control_queue=control_queue,
                wait_task=asyncio.current_task(),
            )
            self.manager.runs[context.run_id] = state
        else:
            state.runner_kind = "logical"
            state.status = "running"
            state.control_queue = control_queue
            state.wait_task = asyncio.current_task()
            state.pack = context.pack
        return state

    async def run_logical_module_lane_dag(
        self,
        parent_state: Any,
        module_packs: dict[str, TaskContextPack],
        depends_on: dict[str, list[str]],
        *,
        task: Callable[[str, LogicalCoderContext], Awaitable[dict[str, Any]] | dict[str, Any]],
        reason: str = "",
        fail_fast: bool = True,
        slot_wait_timeout_seconds: Any = None,
    ) -> dict[str, Any]:
        module_ids = [str(item).strip() for item in module_packs if str(item).strip()]
        unknown = {
            module_id: [dep for dep in _string_list(depends_on.get(module_id)) if dep not in module_packs]
            for module_id in module_ids
        }
        unknown = {module_id: deps for module_id, deps in unknown.items() if deps}
        if unknown:
            return {"status": "failed", "reason": "unknown_dependencies", "unknown_dependencies": unknown}
        pending = list(module_ids)
        running: dict[str, asyncio.Task[dict[str, Any]]] = {}
        completed: dict[str, dict[str, Any]] = {}
        failed: dict[str, dict[str, Any]] = {}
        waiting_for_slot: dict[str, dict[str, Any]] = {}

        while pending or running:
            waiting_for_slot = {}
            ready = [
                module_id
                for module_id in list(pending)
                if all(dep in completed for dep in _string_list(depends_on.get(module_id)))
            ]
            available_attempts = self._available_logical_slot_attempts()
            for module_id in ready:
                if available_attempts is not None and available_attempts <= 0:
                    break
                grant = self.request_logical_slot(
                    parent_state,
                    resource="logical_minion_slot",
                    module_id=module_id,
                    reason=reason or "logical_module_lane_dag",
                )
                if str(grant.get("status") or "") != "granted":
                    waiting_for_slot[module_id] = {"status": "waiting_for_slot", "module_id": module_id, "grant": grant}
                    break
                pending.remove(module_id)
                running[module_id] = asyncio.create_task(
                    self._run_logical_module_lane_with_grant(
                        parent_state,
                        module_packs[module_id],
                        module_id=module_id,
                        task=lambda context, module_id=module_id: task(module_id, context),
                        grant=grant,
                    ),
                    name=f"minion-logical-coder-{module_id}",
                )
                available_attempts = self._available_logical_slot_attempts()
            if not running:
                if ready:
                    slot_wait = await self.wait_logical_slot(
                        parent_state,
                        resource="logical_minion_slot",
                        module_id=str((list(waiting_for_slot) or ready)[0] or ""),
                        reason=reason or "logical_module_lane_dag",
                        timeout_seconds=slot_wait_timeout_seconds,
                    )
                    if str(slot_wait.get("status") or "") in {"available", "notified"}:
                        continue
                    return {
                        "status": "waiting_for_slot",
                        "pending_modules": list(pending),
                        "ready_modules": list(waiting_for_slot) or ready,
                        "completed_modules": list(completed),
                        "failed_modules": list(failed),
                        "waiting_for_slot": waiting_for_slot,
                        "slot_wait": slot_wait,
                    }
                blocked = {
                    module_id: [dep for dep in _string_list(depends_on.get(module_id)) if dep in failed]
                    for module_id in pending
                }
                blocked = {module_id: deps for module_id, deps in blocked.items() if deps}
                return {
                    "status": "failed",
                    "reason": "dependency_failed" if blocked else "dependency_cycle_or_unreachable",
                    "pending_modules": list(pending),
                    "completed": completed,
                    "failed": failed,
                    "blocked": blocked,
                }
            done, _pending_tasks = await asyncio.wait(running.values(), return_when=asyncio.FIRST_COMPLETED)
            for finished in done:
                module_id = next(key for key, value in running.items() if value is finished)
                del running[module_id]
                result = finished.result()
                if str(result.get("status") or "") == "completed":
                    completed[module_id] = result
                elif str(result.get("status") or "") == "waiting_for_slot":
                    waiting_for_slot[module_id] = result
                else:
                    failed[module_id] = result
            if failed and fail_fast:
                for task_item in running.values():
                    task_item.cancel()
                if running:
                    await asyncio.gather(*running.values(), return_exceptions=True)
                return {
                    "status": "failed",
                    "reason": "module_failed",
                    "completed": completed,
                    "failed": failed,
                    "pending_modules": list(pending),
                }

        return {
            "status": "completed" if not failed else "failed",
            "completed": completed,
            "failed": failed,
            "completed_modules": list(completed),
            "failed_modules": list(failed),
        }

    def _logical_minion_dag_result_from_returncodes(self, result: dict[str, Any]) -> dict[str, Any]:
        payload = copy.deepcopy(dict(result))
        completed = dict(payload.get("completed") or {})
        failed = dict(payload.get("failed") or {})
        for module_id, module_result in list(completed.items()):
            returncode = int(dict(module_result.get("result") or {}).get("returncode") or 0)
            if returncode == 0:
                continue
            failed_result = dict(module_result)
            failed_result["status"] = "failed"
            failed_result["reason"] = "logical_minion_nonzero_returncode"
            failed[module_id] = failed_result
            completed.pop(module_id, None)
        payload["completed"] = completed
        payload["failed"] = failed
        payload["completed_modules"] = list(completed)
        payload["failed_modules"] = list(failed)
        if failed:
            payload["status"] = "failed"
        elif str(payload.get("status") or "") == "completed":
            payload["status"] = "completed"
        if failed and "reason" not in payload:
            payload["reason"] = "logical_minion_nonzero_returncode"
        return payload

    def build_logical_coder_context(
        self,
        parent_state: Any,
        pack: TaskContextPack,
        *,
        module_id: str,
        slot_id: str,
    ) -> LogicalCoderContext:
        # Coroutine coders may share a Python process, but they must not share
        # TaskContextPack internals. Deep-copy the payload before attaching
        # scheduler metadata so later milestone updates cannot bleed sideways.
        logical_run_id = f"logical_run_{uuid4().hex[:12]}"
        pack_payload = copy.deepcopy(pack.to_dict())
        metadata = dict(pack_payload.get("metadata") or {})
        metadata.update(
            {
                "logical_parent_run_id": parent_state.run_id,
                "logical_run_id": logical_run_id,
                "logical_module_id": str(module_id or "").strip(),
                "logical_slot_id": str(slot_id or "").strip(),
            }
        )
        pack_payload["metadata"] = metadata
        return LogicalCoderContext(
            run_id=logical_run_id,
            parent_run_id=parent_state.run_id,
            work_order_id=str(pack.work_order_id or ""),
            module_id=str(module_id or "").strip(),
            slot_id=str(slot_id or "").strip(),
            pack=TaskContextPack.from_dict(pack_payload),
        )

    def _logical_runner_event(self, context: LogicalCoderContext, event: dict[str, Any]) -> dict[str, Any]:
        normalized = {
            "event_kind": str(event.get("event_kind") or ""),
            "minion_id": str(event.get("minion_id") or ""),
            "run_id": str(event.get("run_id") or context.run_id),
            "work_order_id": str(event.get("work_order_id") or context.work_order_id),
            "minion_profile": str(event.get("minion_profile") or context.pack.minion_profile),
            "payload": dict(event.get("payload") or {}),
            "created_at": str(event.get("created_at") or utc_now()),
        }
        payload = dict(normalized["payload"])
        metadata = dict(payload.get("metadata") or {})
        metadata.setdefault("logical_parent_run_id", context.parent_run_id)
        metadata.setdefault("logical_run_id", context.run_id)
        metadata.setdefault("logical_module_id", context.module_id)
        metadata.setdefault("logical_slot_id", context.slot_id)
        payload["metadata"] = metadata
        normalized["payload"] = payload
        return normalized

    def _record_logical_runner_event(self, event: dict[str, Any]) -> None:
        if bool(getattr(self.manager, "_should_queue_event_delivery", lambda _event: True)(event)):
            self.manager._queue_event_delivery(event)
        if str(event.get("event_kind") or "") in {"progress", "phase_started"}:
            return
        self.manager.tasking_repository.record_minion_event(event)

    def _max_parallel_modules(self) -> int:
        return int(self.manager.max_parallel_modules or _DEFAULT_MAX_PARALLEL_MODULES)

    def _max_parallel_llm_nodes(self) -> int:
        return int(self.manager.max_parallel_modules or _DEFAULT_MAX_PARALLEL_LLM_NODES)

    def _available_llm_node_slots(self) -> int:
        available = getattr(self.manager, "_available_llm_node_slots", None)
        if callable(available):
            return int(available())
        return int(self.manager._available_module_slots())

    def _active_llm_node_count(self) -> int:
        active = getattr(self.manager, "_active_llm_node_slot_count_from_ledger", None)
        if callable(active):
            return int(active())
        return int(self.manager._active_module_child_count())


def _active_child_work_order_ids_from_plan_execution(plan_execution: dict[str, Any]) -> list[str]:
    values = _string_list(plan_execution.get("active_child_work_order_ids"))
    if not values:
        running_modules = dict(_plan_execution_dag_state(plan_execution).get("running_modules") or {})
        values = _string_list(running_modules.values())
    active_child_work_order_id = str(plan_execution.get("active_child_work_order_id") or "").strip()
    if active_child_work_order_id and active_child_work_order_id not in values:
        values.insert(0, active_child_work_order_id)
    return _dedupe_strings(values)


def _plan_execution_dag_state(plan_execution: dict[str, Any]) -> dict[str, Any]:
    return _dag_state_to_runtime_dict(dict(plan_execution.get("dag_state") or plan_execution.get("module_dag") or {}))


def _optional_timeout_seconds(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, timeout)

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from pal.minion.ipc import MinionManagerClient
from pal.minion.repository import MinionTaskingRepository
from pal.minion.step_runner import ModuleStepRunner, RpcLogicalSlotBroker
from pal.shared import TaskContextPack


@dataclass
class StepProcessRuntime:
    runtime_root: Path
    client: MinionManagerClient = field(init=False)
    tasking_repository: MinionTaskingRepository = field(init=False)

    def __post_init__(self) -> None:
        self.runtime_root = Path(self.runtime_root)
        self.client = MinionManagerClient(runtime_root=self.runtime_root)
        self.tasking_repository = MinionTaskingRepository(runtime_root=self.runtime_root)

    def _available_module_slots(self) -> int:
        return 0

    def _active_module_child_count(self) -> int:
        return 0

    def _allocated_logical_slot_count(self) -> int:
        return 0

    def _should_queue_event_delivery(self, _event: dict[str, Any]) -> bool:
        return False

    def _queue_event_delivery(self, _event: dict[str, Any]) -> None:
        return None


@dataclass
class StepProcessRunner:
    runtime_root: Path
    payload: dict[str, Any]
    _broker: RpcLogicalSlotBroker | None = field(default=None, init=False, repr=False)
    _parent_run_id: str = field(default="", init=False, repr=False)

    async def run(self) -> dict[str, Any]:
        self._parent_run_id = str(self.payload.get("parent_run_id") or "step_process_parent").strip()
        runtime = StepProcessRuntime(runtime_root=Path(self.runtime_root))
        self._broker = RpcLogicalSlotBroker(runtime.client)
        try:
            mode = str(self.payload.get("mode") or "noop").strip() or "noop"
            if mode != "noop":
                return {"status": "failed", "reason": "unsupported_step_process_mode", "mode": mode}
            return await self._run_noop_dag(runtime, self._broker)
        finally:
            if self._broker is not None and self._parent_run_id:
                self._broker.release_for_run(self._parent_run_id, reason="step_process_exit")

    async def _run_noop_dag(self, runtime: StepProcessRuntime, broker: RpcLogicalSlotBroker) -> dict[str, Any]:
        step_runner = ModuleStepRunner(runtime, slot_broker=broker)
        parent_state = SimpleNamespace(
            run_id=self._parent_run_id or "step_process_parent",
            pack=TaskContextPack(
                work_order_id=str(self.payload.get("parent_work_order_id") or "wo_step_process_parent"),
                goal=str(self.payload.get("goal") or "step process parent"),
            ),
        )
        modules = _module_packs_from_payload(self.payload.get("modules"))
        depends_on = _depends_on_from_payload(self.payload.get("depends_on"), module_ids=set(modules))
        delay_seconds = max(0.0, float(self.payload.get("noop_delay_seconds") or 0.0))

        async def task(module_id: str, context) -> dict[str, Any]:
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            return {
                "module_id": module_id,
                "logical_run_id": context.run_id,
                "work_order_id": context.work_order_id,
            }

        result = await step_runner.run_logical_coder_dag(
            parent_state,
            modules,
            depends_on,
            task=task,
            reason=str(self.payload.get("reason") or "step_process_noop_dag"),
        )
        return {"mode": "noop", **dict(result)}


def _module_packs_from_payload(raw: Any) -> dict[str, TaskContextPack]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("step process payload.modules must be a non-empty object")
    modules: dict[str, TaskContextPack] = {}
    for key, value in raw.items():
        module_id = str(key or "").strip()
        if not module_id:
            raise ValueError("step process module id is required")
        if isinstance(value, TaskContextPack):
            modules[module_id] = value
        elif isinstance(value, dict):
            modules[module_id] = TaskContextPack.from_dict(value)
        else:
            raise ValueError(f"step process module {module_id} must be a TaskContextPack object")
    return modules


def _depends_on_from_payload(raw: Any, *, module_ids: set[str]) -> dict[str, list[str]]:
    if raw is None:
        return {module_id: [] for module_id in sorted(module_ids)}
    if not isinstance(raw, dict):
        raise ValueError("step process payload.depends_on must be an object")
    depends_on: dict[str, list[str]] = {}
    for module_id in module_ids:
        value = raw.get(module_id, [])
        if isinstance(value, str):
            deps = [value] if value.strip() else []
        else:
            deps = [str(item).strip() for item in list(value or []) if str(item).strip()]
        depends_on[module_id] = deps
    return depends_on


async def run_step_process_payload(runtime_root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    return await StepProcessRunner(runtime_root=Path(runtime_root), payload=dict(payload)).run()

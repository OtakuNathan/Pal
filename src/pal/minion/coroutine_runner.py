from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

from pal.foundation import utc_now
from pal.minion.lifecycle import ACTIVE_RUN_STATUSES, TERMINAL_RUN_STATUSES


@dataclass
class CoroutineRunnerSupervisor:
    manager: Any

    async def close_all(self) -> None:
        manager = self.manager
        for state in list(manager.runs.values()):
            await self.stop_runner(state)
            if state.status in ACTIVE_RUN_STATUSES:
                manager._record_event(
                    state,
                    {
                        "event_kind": "terminal",
                        "payload": {"status": "killed", "summary": "minion manager shutdown", "reason": "manager_shutdown"},
                        "created_at": utc_now(),
                    },
                )

    async def start_runner(self, state: Any) -> None:
        state.runner_kind = "coroutine"
        state.control_queue = asyncio.Queue()
        self.manager._transition_run_status(state, "running")
        state.wait_task = asyncio.create_task(self._run_coroutine(state), name=f"minion-coroutine-{state.run_id}")

    async def _run_coroutine(self, state: Any) -> None:
        async def write_event(event: dict[str, Any]) -> None:
            self.manager._record_event(state, event)

        async def read_decision(timeout: float | None = None) -> dict[str, Any] | None:
            queue = state.control_queue
            if queue is None:
                return None
            if timeout is None:
                return await queue.get()
            try:
                return await asyncio.wait_for(queue.get(), timeout=max(0.0, float(timeout or 0.0)))
            except asyncio.TimeoutError:
                return None

        from pal.minion.runner import MinionRunner

        runner = MinionRunner(
            runtime_root=self.manager.runtime_root,
            pack=state.pack,
            minion_id=state.minion_id,
            run_id=state.run_id,
            write_event=write_event,
            read_decision=read_decision,
        )
        try:
            returncode = await runner.run()
        except asyncio.CancelledError:
            state.returncode = 2
            if state.status in ACTIVE_RUN_STATUSES:
                self.manager._record_event(
                    state,
                    {
                        "event_kind": "terminal",
                        "payload": {"status": "killed", "summary": "coroutine minion runner cancelled"},
                        "created_at": utc_now(),
                    },
                )
            raise
        except Exception as exc:
            state.returncode = 1
            state.last_error = f"{exc.__class__.__name__}: {exc}"
            if state.status in ACTIVE_RUN_STATUSES:
                self.manager._record_event(
                    state,
                    {
                        "event_kind": "terminal",
                        "payload": {"status": "failed", "summary": "coroutine minion runner failed", "error": state.last_error},
                        "created_at": utc_now(),
                    },
                )
            return
        state.returncode = int(returncode or 0)
        if state.status in TERMINAL_RUN_STATUSES:
            return
        status = "completed" if int(returncode or 0) == 0 else "failed"
        self.manager._record_event(
            state,
            {
                "event_kind": "terminal",
                "payload": {"status": status, "summary": f"minion exited with code {returncode}", "returncode": returncode},
                "created_at": utc_now(),
            },
        )

    async def send_runner_control(self, state: Any, message: dict[str, Any]) -> dict[str, Any]:
        reason = self.runner_control_unavailable_reason(state)
        if reason:
            raise RuntimeError(reason)
        assert state.control_queue is not None
        await state.control_queue.put(dict(message))
        return {"ok": True, "run_id": state.run_id, "message_type": str(message.get("type") or "")}

    async def stop_runner(self, state: Any) -> None:
        task = state.wait_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def read_runner_stdout(self, state: Any) -> None:
        _ = state

    async def read_runner_stderr(self, state: Any) -> None:
        _ = state

    async def wait_runner(self, state: Any) -> None:
        task = state.wait_task
        if task is not None:
            await task

    async def wait_for_stream_tasks(self, state: Any) -> None:
        _ = state

    def record_runner_exit(self, state: Any, returncode: int | None) -> None:
        state.returncode = returncode

    def reconcile_runs(self) -> None:
        for state in list(self.manager.runs.values()):
            task = state.wait_task
            self.consume_task_result(state, task, "coroutine")
            if task is not None and task.done():
                state.wait_task = None

    def active_runner_work_order_ids(self) -> set[str]:
        return {
            str(state.pack.work_order_id)
            for state in self.manager.runs.values()
            if state.runner_kind == "coroutine"
            and state.status in ACTIVE_RUN_STATUSES
            and state.wait_task is not None
            and not state.wait_task.done()
            and str(state.pack.work_order_id or "").strip()
        }

    def find_active_run_for_work_order(self, work_order_id: str) -> Any | None:
        wanted = str(work_order_id or "").strip()
        if not wanted:
            return None
        for state in self.manager.runs.values():
            if str(state.pack.work_order_id or "") != wanted:
                continue
            if (
                state.runner_kind == "coroutine"
                and state.status in ACTIVE_RUN_STATUSES
                and state.wait_task is not None
                and not state.wait_task.done()
            ):
                return state
        return None

    def runner_control_unavailable_reason(self, state: Any) -> str:
        if state.status in TERMINAL_RUN_STATUSES:
            return f"run status is terminal: {state.status}"
        if state.runner_kind != "coroutine":
            return "run is not a coroutine runner"
        if state.control_queue is None:
            return "coroutine control queue is not available"
        if state.wait_task is None or state.wait_task.done():
            return "coroutine runner is not active"
        return ""

    def consume_task_result(self, state: Any, task: asyncio.Task[None] | None, task_name: str) -> None:
        if task is None or not task.done():
            return
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            state.last_error = f"{exc.__class__.__name__}: {exc}"
            self.manager.logger.exception("minion %s background %s task failed", state.run_id, task_name)
            if state.status in ACTIVE_RUN_STATUSES:
                self.manager._record_event(
                    state,
                    {
                        "event_kind": "terminal",
                        "payload": {
                            "status": "failed",
                            "summary": "coroutine minion task failed",
                            "error": state.last_error,
                        },
                        "created_at": utc_now(),
                    },
                )

    def close_liveness_pipe(self, state: Any) -> None:
        _ = state

    def cleanup_runner_payload(self, state: Any) -> None:
        _ = state

    def record_runner_stderr_line(self, state: Any, line: str) -> None:
        _ = state, line

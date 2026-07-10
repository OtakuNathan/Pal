from __future__ import annotations

import asyncio
import contextlib
import sys
from dataclasses import dataclass, field
from typing import Any

from pal.foundation import utc_now
from pal.foundation.sidecar import pack_sidecar_message, read_sidecar_message
from pal.minion.ipc import python_subprocess_env
from pal.minion.lifecycle import ACTIVE_RUN_STATUSES, TERMINAL_RUN_STATUSES


@dataclass
class StepExecutorProcessState:
    executor_key: str
    process: asyncio.subprocess.Process
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    wait_task: asyncio.Task[None] | None = None
    idle_close_task: asyncio.Task[None] | None = None
    run_futures: dict[str, asyncio.Future[None]] = field(default_factory=dict)
    stderr_tail: list[str] = field(default_factory=list)


@dataclass
class StepExecutorRunnerSupervisor:
    manager: Any
    executors: dict[str, StepExecutorProcessState] = field(default_factory=dict)
    _closing_executor_keys: set[str] = field(default_factory=set)

    async def close_all(self) -> None:
        for executor in list(self.executors.values()):
            await self._close_executor(executor, reason="manager_shutdown")
        for state in list(self.manager.runs.values()):
            if state.status in ACTIVE_RUN_STATUSES:
                self.manager._record_event(
                    state,
                    {
                        "event_kind": "terminal",
                        "payload": {"status": "killed", "summary": "minion manager shutdown", "reason": "manager_shutdown"},
                        "created_at": utc_now(),
                    },
                )
                self._finish_run_future(state.run_id)

    async def start_runner(self, state: Any) -> None:
        executor = await self._ensure_executor_started(self._executor_key_for_state(state))
        state.runner_kind = "step_process"
        state.process = executor.process
        state.stderr_tail = list(executor.stderr_tail[-20:])
        self.manager._transition_run_status(state, "running")
        future = asyncio.get_running_loop().create_future()
        executor.run_futures[state.run_id] = future
        state.wait_task = asyncio.create_task(self._wait_run(executor.executor_key, state.run_id), name=f"minion-step-executor-wait-{state.run_id}")
        await self._write(
            executor,
            {
                "type": "start_run",
                "run_id": state.run_id,
                "minion_id": state.minion_id,
                "task_context_pack": state.pack.to_dict(),
            },
        )

    async def _ensure_executor_started(self, executor_key: str) -> StepExecutorProcessState:
        existing = self.executors.get(executor_key)
        if (
            existing is not None
            and existing.executor_key not in self._closing_executor_keys
            and existing.process.returncode is None
        ):
            return existing
        if existing is not None:
            if existing.process.returncode is None:
                with contextlib.suppress(Exception):
                    await existing.process.wait()
            await self._wait_for_executor_tasks(existing)
            self.executors.pop(executor_key, None)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pal.minion.step_executor_main",
            "--runtime-root",
            str(self.manager.runtime_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=python_subprocess_env(),
        )
        executor = StepExecutorProcessState(executor_key=executor_key, process=process)
        self.executors[executor_key] = executor
        executor.stdout_task = asyncio.create_task(self._read_stdout(executor), name=f"minion-step-executor-stdout-{executor_key}")
        executor.stderr_task = asyncio.create_task(self._read_stderr(executor), name=f"minion-step-executor-stderr-{executor_key}")
        executor.wait_task = asyncio.create_task(self._wait_executor(executor), name=f"minion-step-executor-wait-{executor_key}")
        return executor

    async def _close_executor(self, executor: StepExecutorProcessState, *, reason: str) -> None:
        self._closing_executor_keys.add(executor.executor_key)
        if executor.process.returncode is None:
            with contextlib.suppress(Exception):
                await self._write(executor, {"type": "shutdown", "reason": reason})
            try:
                await asyncio.wait_for(executor.process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    executor.process.terminate()
                try:
                    await asyncio.wait_for(executor.process.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        executor.process.kill()
                    await executor.process.wait()
        await self._wait_for_executor_tasks(executor)
        self._mark_active_runs_killed_for_executor(executor, reason=reason)
        self._closing_executor_keys.discard(executor.executor_key)
        self.executors.pop(executor.executor_key, None)

    async def _write(self, executor: StepExecutorProcessState, message: dict[str, Any]) -> None:
        process = executor.process
        if process.stdin is None or process.returncode is not None:
            raise RuntimeError("step executor process is not accepting commands")
        process.stdin.write(pack_sidecar_message(dict(message)))
        await process.stdin.drain()

    async def _read_stdout(self, executor: StepExecutorProcessState) -> None:
        process = executor.process
        if process.stdout is None:
            return
        try:
            while True:
                payload = await read_sidecar_message(process.stdout)
                if str(payload.get("type") or "") == "event":
                    self._record_executor_event(executor, dict(payload))
        except asyncio.IncompleteReadError:
            return
        except Exception:
            self.manager.logger.exception("failed to read minion step executor stdout: %s", executor.executor_key)

    async def _read_stderr(self, executor: StepExecutorProcessState) -> None:
        process = executor.process
        if process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                line_text = line.decode("utf-8", errors="replace").rstrip()
                if line_text:
                    executor.stderr_tail.append(line_text)
                    del executor.stderr_tail[:-50]
                    for state in self._active_states_for_executor(executor.executor_key):
                        state.stderr_tail = list(executor.stderr_tail[-20:])
                    self.manager.logger.info("minion step executor %s stderr: %s", executor.executor_key, line_text)
        except Exception:
            return

    async def _wait_executor(self, executor: StepExecutorProcessState) -> None:
        await executor.process.wait()
        await self._wait_for_executor_tasks(executor, exclude=asyncio.current_task())
        if executor.executor_key in self._closing_executor_keys:
            self._mark_active_runs_killed_for_executor(executor, reason="manager_shutdown")
            return
        self._mark_active_runs_failed_after_executor_exit(executor, executor.process.returncode)

    async def _wait_for_executor_tasks(
        self,
        executor: StepExecutorProcessState,
        *,
        exclude: asyncio.Task[Any] | None = None,
    ) -> None:
        tasks = [
            task
            for task in (executor.stdout_task, executor.stderr_task, executor.wait_task)
            if task is not None and task is not exclude and not task.done()
        ]
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=1.0)
            for task in done:
                self.consume_task_result(None, task, "step_executor")
            for task in pending:
                task.cancel()
        executor.stdout_task = None if executor.stdout_task is exclude or executor.stdout_task is None or executor.stdout_task.done() else executor.stdout_task
        executor.stderr_task = None if executor.stderr_task is exclude or executor.stderr_task is None or executor.stderr_task.done() else executor.stderr_task
        executor.wait_task = None if executor.wait_task is exclude or executor.wait_task is None or executor.wait_task.done() else executor.wait_task

    def _record_executor_event(self, executor: StepExecutorProcessState, event: dict[str, Any]) -> None:
        run_id = str(event.get("run_id") or "").strip()
        state = self.manager.runs.get(run_id)
        if state is None:
            self.manager.logger.warning("step executor emitted event for unknown run: %s", run_id)
            return
        if str(event.get("event_kind") or "") == "terminal":
            payload = dict(event.get("payload") or {})
            if "returncode" in payload:
                with contextlib.suppress(TypeError, ValueError):
                    state.returncode = int(payload.get("returncode"))
        self.manager._record_event(state, event)
        if str(event.get("event_kind") or "") == "terminal":
            self._finish_run_future(run_id)
            executor.run_futures.pop(run_id, None)
            self._schedule_idle_executor_close(executor, reason="run_terminal")

    def _mark_active_runs_failed_after_executor_exit(self, executor: StepExecutorProcessState, returncode: int | None) -> None:
        for state in list(self._active_states_for_executor(executor.executor_key)):
            payload: dict[str, Any] = {
                "status": "failed",
                "summary": f"step executor exited with code {returncode}",
                "reason": "step_executor_exited",
                "returncode": returncode,
                "executor_key": executor.executor_key,
            }
            state.returncode = returncode
            if executor.stderr_tail:
                payload["stderr_tail"] = list(executor.stderr_tail[-20:])
                payload["error"] = executor.stderr_tail[-1]
            self.manager._record_event(
                state,
                {
                    "event_kind": "terminal",
                    "payload": payload,
                    "created_at": utc_now(),
                },
            )
            self._finish_run_future(state.run_id)
        self.executors.pop(executor.executor_key, None)

    def _mark_active_runs_killed_for_executor(self, executor: StepExecutorProcessState, *, reason: str) -> None:
        for state in list(self._active_states_for_executor(executor.executor_key)):
            self.manager._record_event(
                state,
                {
                    "event_kind": "terminal",
                    "payload": {
                        "status": "killed",
                        "summary": "step executor stopped by manager",
                        "reason": reason or "manager_shutdown",
                        "executor_key": executor.executor_key,
                    },
                    "created_at": utc_now(),
                },
            )
            self._finish_run_future(state.run_id)

    async def send_runner_control(self, state: Any, message: dict[str, Any]) -> dict[str, Any]:
        reason = self.runner_control_unavailable_reason(state)
        if reason:
            raise RuntimeError(reason)
        executor = self.executors[self._executor_key_for_state(state)]
        await self._write(executor, {"type": "control", "run_id": state.run_id, "message": dict(message)})
        return {"ok": True, "run_id": state.run_id, "message_type": str(message.get("type") or "")}

    async def stop_runner(self, state: Any) -> None:
        if state.status in TERMINAL_RUN_STATUSES:
            return
        executor = self.executors.get(self._executor_key_for_state(state))
        if executor is not None:
            with contextlib.suppress(Exception):
                await self._write(executor, {"type": "stop_run", "run_id": state.run_id, "reason": "manager stop"})
            future = executor.run_futures.get(state.run_id)
            if future is not None and not future.done():
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(future, timeout=2.0)

    async def _wait_run(self, executor_key: str, run_id: str) -> None:
        executor = self.executors.get(executor_key)
        future = executor.run_futures.get(run_id) if executor is not None else None
        if future is not None:
            await future

    def reconcile_runs(self) -> None:
        for executor in list(self.executors.values()):
            self.consume_task_result(None, executor.stdout_task, "step_executor_stdout")
            self.consume_task_result(None, executor.stderr_task, "step_executor_stderr")
            self.consume_task_result(None, executor.wait_task, "step_executor_wait")
            if executor.process.returncode is not None:
                self._mark_active_runs_failed_after_executor_exit(executor, executor.process.returncode)
                continue
            if self._executor_is_idle(executor):
                self._schedule_idle_executor_close(executor, reason="idle_reconcile")
        self._mark_active_runs_terminal_from_work_order_status()
        for state in list(self.manager.runs.values()):
            task = state.wait_task
            self.consume_task_result(state, task, "step_executor_run")
            if task is not None and task.done():
                state.wait_task = None

    def active_runner_work_order_ids(self) -> set[str]:
        return {
            str(state.pack.work_order_id)
            for state in self.manager.runs.values()
            if state.runner_kind == "step_process"
            and state.status in ACTIVE_RUN_STATUSES
            and str(state.pack.work_order_id or "").strip()
        }

    def executor_statuses(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for executor in self.executors.values():
            process = executor.process
            active_states = self._active_states_for_executor(executor.executor_key)
            run_future_ids = list(dict(executor.run_futures or {}).keys())
            items.append(
                {
                    "executor_key": executor.executor_key,
                    "executor_pid": process.pid if process is not None else None,
                    "executor_returncode": process.returncode if process is not None else None,
                    "active_coroutine_count": len(run_future_ids),
                    "active_run_ids": [str(state.run_id) for state in active_states],
                    "active_work_order_ids": [str(state.pack.work_order_id) for state in active_states],
                    "run_future_ids": run_future_ids,
                    "idle": self._executor_is_idle(executor),
                    "idle_close_pending": bool(executor.idle_close_task is not None and not executor.idle_close_task.done()),
                }
            )
        return items

    def find_active_run_for_work_order(self, work_order_id: str) -> Any | None:
        wanted = str(work_order_id or "").strip()
        if not wanted:
            return None
        for state in self.manager.runs.values():
            if str(state.pack.work_order_id or "") == wanted and state.runner_kind == "step_process" and state.status in ACTIVE_RUN_STATUSES:
                return state
        return None

    def runner_control_unavailable_reason(self, state: Any) -> str:
        if state.status in TERMINAL_RUN_STATUSES:
            return f"run status is terminal: {state.status}"
        if state.runner_kind != "step_process":
            return "run is not a step process runner"
        executor = self.executors.get(self._executor_key_for_state(state))
        if executor is None or executor.process.returncode is not None:
            return "step executor process is not running"
        if executor.process.stdin is None:
            return "step executor stdin is not available"
        return ""

    def consume_task_result(self, state: Any, task: asyncio.Task[Any] | None, task_name: str) -> None:
        if task is None or not task.done():
            return
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if state is not None:
                state.last_error = f"{exc.__class__.__name__}: {exc}"
            self.manager.logger.exception("minion step executor %s task failed", task_name)

    def record_runner_stderr_line(self, state: Any, line: str) -> None:
        executor = self.executors.get(self._executor_key_for_state(state))
        if executor is not None and line:
            executor.stderr_tail.append(str(line))
            del executor.stderr_tail[:-50]
            state.stderr_tail = list(executor.stderr_tail[-20:])
            return
        if line:
            state.stderr_tail.append(str(line))
            del state.stderr_tail[:-20]

    def _finish_run_future(self, run_id: str) -> None:
        for executor in self.executors.values():
            future = executor.run_futures.get(run_id)
            if future is not None and not future.done():
                future.set_result(None)
                return

    def _mark_active_runs_terminal_from_work_order_status(self) -> None:
        for state in list(self.manager.runs.values()):
            if state.runner_kind != "step_process" or state.status not in ACTIVE_RUN_STATUSES:
                continue
            work_order_id = str(state.pack.work_order_id or "").strip()
            if not work_order_id:
                continue
            try:
                snapshot = self.manager.tasking_repository.read_work_order(work_order_id)
            except Exception:
                continue
            if snapshot.get("status") != "ok":
                continue
            work_order = dict(snapshot.get("work_order") or {})
            status = str(work_order.get("status") or "").strip().lower()
            if status not in TERMINAL_RUN_STATUSES:
                continue
            self.manager._record_event(
                state,
                {
                    "event_kind": "terminal",
                    "payload": {
                        "status": status,
                        "summary": f"work order {work_order_id} is already {status}; reconciling stale runner state",
                        "reason": "work_order_terminal_reconcile",
                    },
                    "created_at": utc_now(),
                },
            )
            self._finish_run_future(state.run_id)
            for executor in self.executors.values():
                executor.run_futures.pop(state.run_id, None)
                self._schedule_idle_executor_close(executor, reason="work_order_terminal_reconcile")

    def _active_states_for_executor(self, executor_key: str) -> list[Any]:
        return [
            state
            for state in self.manager.runs.values()
            if state.runner_kind == "step_process"
            and state.status in ACTIVE_RUN_STATUSES
            and self._executor_key_for_state(state) == executor_key
        ]

    def _executor_is_idle(self, executor: StepExecutorProcessState) -> bool:
        return not dict(getattr(executor, "run_futures", {}) or {}) and not self._active_states_for_executor(executor.executor_key)

    def _schedule_idle_executor_close(self, executor: StepExecutorProcessState, *, reason: str) -> None:
        if executor.executor_key in self._closing_executor_keys:
            return
        if not self._executor_is_idle(executor):
            return
        close_task = getattr(executor, "idle_close_task", None)
        if close_task is not None and not close_task.done():
            return
        process = getattr(executor, "process", None)
        if process is None or getattr(process, "returncode", None) is not None:
            self.executors.pop(executor.executor_key, None)
            return
        if not callable(getattr(process, "wait", None)):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        executor.idle_close_task = loop.create_task(
            self._close_executor_if_still_idle(executor.executor_key, reason=reason),
            name=f"minion-step-executor-idle-close-{executor.executor_key}",
        )

    async def _close_executor_if_still_idle(self, executor_key: str, *, reason: str) -> None:
        await asyncio.sleep(0)
        executor = self.executors.get(executor_key)
        if executor is None:
            return
        executor.idle_close_task = None
        if not self._executor_is_idle(executor):
            return
        await self._close_executor(executor, reason=reason or "executor_idle")

    def _executor_key_for_state(self, state: Any) -> str:
        metadata = dict(getattr(state.pack, "metadata", {}) or {})
        return (
            str(metadata.get("step_executor_key") or "").strip()
            or str(metadata.get("executor_key") or "").strip()
            or str(metadata.get("parent_work_order_id") or "").strip()
            or str(getattr(state.pack, "work_order_id", "") or "").strip()
            or str(getattr(state, "run_id", "") or "").strip()
            or "default"
        )

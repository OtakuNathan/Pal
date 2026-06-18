from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from dataclasses import dataclass
from typing import Any

from pal.foundation import utc_now
from pal.foundation.sidecar import pack_sidecar_message, read_sidecar_message
from pal.minion.ipc import python_subprocess_env
from pal.minion.lifecycle import ACTIVE_RUN_STATUSES, TERMINAL_RUN_STATUSES
from pal.minion.sandbox import build_sandboxed_runner_invocation


def runner_stderr_line_is_error(line: str) -> bool:
    text = str(line or "").strip()
    if not text:
        return False
    if text.startswith("[tool call]"):
        return False
    if text.startswith("[tool result]"):
        return " ok=False " in text or " status=error " in text or " status=failed " in text
    lowered = text.lower()
    return (
        text.startswith("Traceback")
        or "traceback (most recent call last)" in lowered
        or lowered.startswith("error:")
        or " error " in lowered
        or "exception" in lowered
    )


@dataclass
class RunnerProcessSupervisor:
    manager: Any

    async def close_all(self) -> None:
        manager = self.manager
        for state in list(manager.runs.values()):
            self.close_liveness_pipe(state)
            if state.process is not None and state.process.returncode is None:
                with contextlib.suppress(Exception):
                    state.process.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(state.process.wait(), timeout=1.0)
                if state.process.returncode is None:
                    with contextlib.suppress(Exception):
                        state.process.kill()
                    with contextlib.suppress(Exception):
                        await state.process.wait()
            with contextlib.suppress(Exception):
                await self.wait_for_stream_tasks(state)
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
        manager = self.manager
        read_fd, write_fd = os.pipe()
        argv = [
            sys.executable,
            "-m",
            "pal.minion.runner_main",
            "--runtime-root",
            str(manager.runtime_root),
            "--task-json",
            state.pack.to_json(),
            "--minion-id",
            state.minion_id,
            "--run-id",
            state.run_id,
            "--manager-liveness-fd",
            str(read_fd),
        ]
        argv, env = build_sandboxed_runner_invocation(
            runtime_root=manager.runtime_root,
            pack=state.pack,
            argv=argv,
            env=python_subprocess_env(),
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                pass_fds=(read_fd,),
            )
        except Exception:
            with contextlib.suppress(OSError):
                os.close(read_fd)
            with contextlib.suppress(OSError):
                os.close(write_fd)
            raise
        with contextlib.suppress(OSError):
            os.close(read_fd)
        state.process = process
        state.manager_liveness_write_fd = write_fd
        manager._transition_run_status(state, "running")
        state.stdout_task = asyncio.create_task(self.read_runner_stdout(state), name=f"minion-stdout-{state.run_id}")
        state.stderr_task = asyncio.create_task(self.read_runner_stderr(state), name=f"minion-stderr-{state.run_id}")
        state.wait_task = asyncio.create_task(self.wait_runner(state), name=f"minion-wait-{state.run_id}")

    async def send_runner_control(self, state: Any, message: dict[str, Any]) -> dict[str, Any]:
        if state.process is None or state.process.stdin is None or state.process.returncode is not None:
            raise RuntimeError(f"minion is not accepting manager control messages: {state.run_id}")
        state.process.stdin.write(pack_sidecar_message(dict(message)))
        await state.process.stdin.drain()
        return {"ok": True, "run_id": state.run_id, "message_type": str(message.get("type") or "")}

    async def read_runner_stdout(self, state: Any) -> None:
        process = state.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                payload = await read_sidecar_message(process.stdout)
                if str(payload.get("type") or "") != "event":
                    continue
                self.manager._record_event(state, payload)
        except asyncio.IncompleteReadError:
            return
        except Exception as exc:
            state.last_error = f"{exc.__class__.__name__}: {exc}"
            self.manager.logger.exception("failed to read minion stdout: %s", state.run_id)

    async def read_runner_stderr(self, state: Any) -> None:
        process = state.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                line_text = line.decode("utf-8", errors="replace").rstrip()
                self.record_runner_stderr_line(state, line_text)
                self.manager.logger.info("minion %s stderr: %s", state.run_id, line_text)
        except Exception:
            return

    async def wait_runner(self, state: Any) -> None:
        process = state.process
        if process is None:
            return
        returncode = await process.wait()
        await self.wait_for_stream_tasks(state)
        self.record_runner_exit(state, returncode)

    async def wait_for_stream_tasks(self, state: Any) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in (state.stdout_task, state.stderr_task)
            if task is not None and task is not current and not task.done()
        ]
        if tasks:
            done, _pending = await asyncio.wait(tasks, timeout=1.0)
            for task in done:
                self.consume_task_result(state, task, "stream")

    def record_runner_exit(self, state: Any, returncode: int | None) -> None:
        self.close_liveness_pipe(state)
        if state.status in TERMINAL_RUN_STATUSES:
            return
        status = "completed" if returncode == 0 else "failed"
        payload: dict[str, Any] = {
            "status": status,
            "summary": f"minion exited with code {returncode}",
            "returncode": returncode,
        }
        if state.last_error:
            payload["error"] = state.last_error
        if state.stderr_tail:
            payload["stderr_tail"] = list(state.stderr_tail[-20:])
        self.manager._record_event(
            state,
            {
                "event_kind": "terminal",
                "payload": payload,
                "created_at": utc_now(),
            },
        )

    def reconcile_runs(self) -> None:
        manager = self.manager
        for state in list(manager.runs.values()):
            for task_name, task in (
                ("stdout", state.stdout_task),
                ("stderr", state.stderr_task),
                ("wait", state.wait_task),
            ):
                self.consume_task_result(state, task, task_name)
                if task is not None and task.done():
                    setattr(state, f"{task_name}_task", None)
            process = state.process
            returncode = getattr(process, "returncode", None) if process is not None else None
            if returncode is not None and state.status in ACTIVE_RUN_STATUSES:
                self.record_runner_exit(state, returncode)

    def active_runner_work_order_ids(self) -> set[str]:
        return {
            str(state.pack.work_order_id)
            for state in self.manager.runs.values()
            if state.status in ACTIVE_RUN_STATUSES
            and state.process is not None
            and state.process.returncode is None
            and str(state.pack.work_order_id or "").strip()
        }

    def find_active_run_for_work_order(self, work_order_id: str) -> Any | None:
        wanted = str(work_order_id or "").strip()
        if not wanted:
            return None
        for state in self.manager.runs.values():
            if str(state.pack.work_order_id or "") != wanted:
                continue
            if state.status in ACTIVE_RUN_STATUSES and state.process is not None and state.process.returncode is None:
                return state
        return None

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
            if task_name == "wait" and state.status in ACTIVE_RUN_STATUSES:
                self.manager._record_event(
                    state,
                    {
                        "event_kind": "terminal",
                        "payload": {
                            "status": "failed",
                            "summary": "minion wait task failed",
                            "error": state.last_error,
                        },
                        "created_at": utc_now(),
                    },
                )

    def close_liveness_pipe(self, state: Any) -> None:
        fd = state.manager_liveness_write_fd
        if fd is None:
            return
        state.manager_liveness_write_fd = None
        with contextlib.suppress(OSError):
            os.close(fd)

    def record_runner_stderr_line(self, state: Any, line: str) -> None:
        normalized = str(line or "").strip()
        if not normalized:
            return
        state.stderr_tail.append(normalized)
        if len(state.stderr_tail) > 20:
            del state.stderr_tail[:-20]
        if runner_stderr_line_is_error(normalized):
            state.last_error = normalized

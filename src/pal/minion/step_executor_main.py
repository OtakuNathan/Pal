from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from pathlib import Path
from typing import Any

from pal.foundation import utc_now
from pal.foundation.sidecar import pack_sidecar_message, read_sidecar_message_sync
from pal.shared import TaskContextPack


_PROTOCOL_STDOUT = getattr(sys.stdout, "buffer", sys.stdout)
_STDOUT_REDIRECT: Any | None = None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pal-minion-step-executor")
    parser.add_argument("--runtime-root", type=Path, required=True)
    return parser


class StepExecutor:
    def __init__(self, *, runtime_root: Path) -> None:
        self.runtime_root = Path(runtime_root)
        self.control_queues: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self.tasks: dict[str, asyncio.Task[int]] = {}
        self._write_lock = asyncio.Lock()
        self._shutdown = asyncio.Event()

    async def run(self) -> int:
        read_task = asyncio.create_task(self._read_commands(), name="minion-step-executor-commands")
        try:
            await self._shutdown.wait()
        finally:
            read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await read_task
            await self._cancel_all("step executor shutting down")
        return 0

    async def _read_commands(self) -> None:
        while True:
            try:
                message = await asyncio.to_thread(read_sidecar_message_sync, sys.stdin.buffer)
            except (EOFError, ValueError):
                self._shutdown.set()
                return
            except Exception as exc:
                await self._write(
                    {
                        "type": "executor_error",
                        "error": f"{exc.__class__.__name__}: {exc}",
                    }
                )
                self._shutdown.set()
                return
            await self._handle_command(dict(message))

    async def _handle_command(self, message: dict[str, Any]) -> None:
        message_type = str(message.get("type") or "").strip()
        if message_type == "start_run":
            await self._start_run(message)
            return
        if message_type == "control":
            await self._send_control(message)
            return
        if message_type == "stop_run":
            await self._stop_run(str(message.get("run_id") or ""), reason=str(message.get("reason") or "manager stop"))
            return
        if message_type == "shutdown":
            self._shutdown.set()
            return

    async def _start_run(self, message: dict[str, Any]) -> None:
        run_id = str(message.get("run_id") or "").strip()
        minion_id = str(message.get("minion_id") or "").strip()
        if not run_id or not minion_id:
            return
        if run_id in self.tasks and not self.tasks[run_id].done():
            return
        pack = TaskContextPack.from_dict(dict(message.get("task_context_pack") or {}))
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.control_queues[run_id] = queue
        self.tasks[run_id] = asyncio.create_task(
            self._run_one(run_id=run_id, minion_id=minion_id, pack=pack, control_queue=queue),
            name=f"minion-step-executor-run-{run_id}",
        )

    async def _run_one(
        self,
        *,
        run_id: str,
        minion_id: str,
        pack: TaskContextPack,
        control_queue: asyncio.Queue[dict[str, Any]],
    ) -> int:
        terminal_emitted = False

        async def write_event(event: dict[str, Any]) -> None:
            nonlocal terminal_emitted
            normalized = {
                "type": "event",
                "event_kind": str(event.get("event_kind") or ""),
                "minion_id": str(event.get("minion_id") or minion_id),
                "run_id": str(event.get("run_id") or run_id),
                "work_order_id": str(event.get("work_order_id") or pack.work_order_id),
                "minion_profile": str(event.get("minion_profile") or pack.minion_profile),
                "payload": dict(event.get("payload") or {}),
                "created_at": str(event.get("created_at") or utc_now()),
            }
            terminal_emitted = terminal_emitted or normalized["event_kind"] == "terminal"
            await self._write(normalized)

        async def read_decision(timeout: float | None = None) -> dict[str, Any] | None:
            if timeout is None:
                return await control_queue.get()
            try:
                return await asyncio.wait_for(control_queue.get(), timeout=max(0.0, float(timeout or 0.0)))
            except asyncio.TimeoutError:
                return None

        from pal.minion.runner import MinionRunner

        try:
            returncode = await MinionRunner(
                runtime_root=self.runtime_root,
                pack=pack,
                minion_id=minion_id,
                run_id=run_id,
                write_event=write_event,
                read_decision=read_decision,
            ).run()
        except asyncio.CancelledError:
            if not terminal_emitted:
                await write_event(
                    {
                        "event_kind": "terminal",
                        "payload": {"status": "killed", "summary": "step executor run cancelled", "reason": "step_executor_cancelled"},
                        "created_at": utc_now(),
                    }
                )
            return 2
        except Exception as exc:
            if not terminal_emitted:
                await write_event(
                    {
                        "event_kind": "terminal",
                        "payload": {
                            "status": "failed",
                            "summary": "step executor run failed",
                            "error": f"{exc.__class__.__name__}: {exc}",
                            "error_type": exc.__class__.__name__,
                        },
                        "created_at": utc_now(),
                    }
                )
            return 1
        except BaseException as exc:
            if not terminal_emitted:
                await write_event(
                    {
                        "event_kind": "terminal",
                        "payload": {
                            "status": "failed",
                            "summary": "step executor run crashed",
                            "error": f"{exc.__class__.__name__}: {exc}",
                            "error_type": exc.__class__.__name__,
                        },
                        "created_at": utc_now(),
                    }
                )
            return 1
        finally:
            self.control_queues.pop(run_id, None)
            self.tasks.pop(run_id, None)
        if not terminal_emitted:
            status = "completed" if int(returncode or 0) == 0 else "failed"
            await write_event(
                {
                    "event_kind": "terminal",
                    "payload": {
                        "status": status,
                        "summary": f"step executor run exited with code {int(returncode or 0)}",
                        "returncode": int(returncode or 0),
                    },
                    "created_at": utc_now(),
                }
            )
        return int(returncode or 0)

    async def _send_control(self, message: dict[str, Any]) -> None:
        run_id = str(message.get("run_id") or "").strip()
        queue = self.control_queues.get(run_id)
        if queue is None:
            return
        await queue.put(dict(message.get("message") or {}))

    async def _stop_run(self, run_id: str, *, reason: str) -> None:
        task = self.tasks.get(run_id)
        queue = self.control_queues.get(run_id)
        if queue is not None:
            await queue.put({"type": "cancel_requested", "payload": {"reason": reason}})
        if task is not None and not task.done():
            task.cancel()

    async def _cancel_all(self, reason: str) -> None:
        for run_id in list(self.tasks):
            await self._stop_run(run_id, reason=reason)
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)

    async def _write(self, payload: dict[str, Any]) -> None:
        async with self._write_lock:
            writer = _PROTOCOL_STDOUT
            writer.write(pack_sidecar_message(dict(payload)))
            writer.flush()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _redirect_stdout_to_stderr()
    return asyncio.run(StepExecutor(runtime_root=args.runtime_root).run())


def _redirect_stdout_to_stderr() -> None:
    global _STDOUT_REDIRECT
    if _STDOUT_REDIRECT is not None:
        return
    stderr = getattr(sys.stderr, "buffer", sys.stderr)
    if not hasattr(stderr, "fileno"):
        return
    encoding = getattr(sys.stderr, "encoding", None) or "utf-8"
    errors = getattr(sys.stderr, "errors", None) or "replace"
    try:
        _STDOUT_REDIRECT = open(stderr.fileno(), "w", buffering=1, encoding=encoding, errors=errors, closefd=False)
    except Exception:
        return
    sys.stdout = _STDOUT_REDIRECT


if __name__ == "__main__":
    raise SystemExit(main())

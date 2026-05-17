from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.foundation import utc_now
from pal.foundation.sidecar import (
    dispatch_sidecar_request,
    pack_sidecar_message,
    read_sidecar_message,
)
from pal.minion.git_env import finalize_work_order_branch, prepare_task_workspace
from pal.minion.ipc import cleanup_manager_endpoint, minion_log_path, minion_runner_log_path, start_manager_server
from pal.minion.ipc import python_subprocess_env
from pal.minion.profiles import MinionProfileRegistry
from pal.minion.repository import MinionTaskingRepository
from pal.shared import MinionApprovalDecision, TaskContextPack


_ACTIVE_RUN_STATUSES = {"starting", "running", "approval_pending", "clarification_pending"}
_TERMINAL_RUN_STATUSES = {"completed", "failed", "blocked", "killed"}


@dataclass
class MinionRunState:
    minion_id: str
    run_id: str
    pack: TaskContextPack
    process: asyncio.subprocess.Process | None = None
    status: str = "starting"
    started_at: str = field(default_factory=utc_now)
    ended_at: str = ""
    last_error: str = ""
    last_event: dict[str, Any] = field(default_factory=dict)
    last_event_at: str = ""
    last_phase: str = ""
    last_tool_call: dict[str, Any] = field(default_factory=dict)
    llm_round_count: int = 0
    tool_call_count: int = 0
    pending_approval: dict[str, Any] = field(default_factory=dict)
    pending_clarification: dict[str, Any] = field(default_factory=dict)
    ledger: list[dict[str, Any]] = field(default_factory=list)
    stderr_tail: list[str] = field(default_factory=list)
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    wait_task: asyncio.Task[None] | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "work_order_id": self.pack.work_order_id,
            "minion_profile": self.pack.minion_profile,
            "profile_display_name": str((self.pack.resolved_profile or {}).get("display_name") or self.pack.minion_profile),
            "status": self.status,
            "pid": self.process.pid if self.process is not None else None,
            "returncode": self.process.returncode if self.process is not None else None,
            "instruction": self.pack.instruction,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "last_error": self.last_error,
            "last_event_at": self.last_event_at,
            "last_phase": self.last_phase,
            "last_tool_call": dict(self.last_tool_call),
            "llm_round_count": self.llm_round_count,
            "tool_call_count": self.tool_call_count,
            "debug_log_path": _debug_log_path_from_pack(self.pack),
            "current_milestone": dict((self.pack.continuity or {}).get("current_milestone") or {}),
            "stderr_tail": list(self.stderr_tail[-20:]),
            "last_event": dict(self.last_event),
            "pending_approval": dict(self.pending_approval),
            "pending_clarification": dict(self.pending_clarification),
        }

    def detail(self) -> dict[str, Any]:
        payload = self.summary()
        payload["task_context_pack"] = self.pack.to_dict()
        payload["ledger"] = list(self.ledger[-100:])
        return payload


@dataclass
class MinionManager:
    runtime_root: Path
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("pal.minion.manager"))
    tasking_repository: MinionTaskingRepository = field(init=False)
    server: asyncio.base_events.Server | None = None
    endpoint_info: dict[str, Any] = field(default_factory=dict)
    runs: dict[str, MinionRunState] = field(default_factory=dict)
    started_at: str = field(default_factory=utc_now)
    event_queue: list[dict[str, Any]] = field(default_factory=list)
    event_subscribers: list[asyncio.StreamWriter] = field(default_factory=list)
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _serial_followups_inflight: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.tasking_repository = MinionTaskingRepository(runtime_root=self.runtime_root)
        self.tasking_repository.ensure_schema()

    async def run(self) -> None:
        self.server, self.endpoint_info = await start_manager_server(self.runtime_root, self._handle_client)
        self.logger.info("minion manager listening: %s", self.endpoint_info)
        async with self.server:
            serve_task = asyncio.create_task(self.server.serve_forever(), name="minion-manager-serve")
            try:
                await self._shutdown_event.wait()
            finally:
                serve_task.cancel()
                self.server.close()
                await self.server.wait_closed()
                with contextlib.suppress(asyncio.CancelledError):
                    await serve_task
                await self.close_all()
                await self._close_event_subscribers()
                await cleanup_manager_endpoint(self.runtime_root)
                self.logger.info("minion manager stopped")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    request = await read_sidecar_message(reader)
                except asyncio.IncompleteReadError:
                    return
                if str(request.get("method") or "") == "subscribe_events":
                    await self._handle_event_subscription(request, reader, writer)
                    return
                response = await self._dispatch(request)
                writer.write(pack_sidecar_message(response))
                await writer.drain()
        except (ConnectionError, OSError, ValueError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        return await dispatch_sidecar_request(request, self._call_method, error_kind=lambda exc: "manager", logger=self.logger)

    async def _handle_event_subscription(self, request: dict[str, Any], reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        request_id = str(request.get("id") or "")
        backlog = list(self.event_queue)
        self.event_queue.clear()
        self.event_subscribers.append(writer)
        try:
            writer.write(
                pack_sidecar_message(
                    {
                        "type": "response",
                        "id": request_id,
                        "ok": True,
                        "result": {"subscribed": True, "backlog_count": len(backlog)},
                    }
                )
            )
            for event in backlog:
                writer.write(pack_sidecar_message({"type": "event", "event": event}))
            await writer.drain()
            while not self._shutdown_event.is_set():
                try:
                    message = await read_sidecar_message(reader)
                except asyncio.IncompleteReadError:
                    return
                if str(message.get("method") or "") == "unsubscribe_events":
                    return
        finally:
            with contextlib.suppress(ValueError):
                self.event_subscribers.remove(writer)

    async def _call_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._reconcile_runs()
        if method == "health":
            return self.health()
        if method == "list_runs":
            return {"items": [state.summary() for state in sorted(self.runs.values(), key=lambda item: item.started_at)]}
        if method == "read_run":
            return self.read_run(str(params.get("run_id") or ""))
        if method == "spawn":
            return await self.spawn(dict(params.get("task_context_pack") or {}))
        if method == "kill":
            return await self.kill(str(params.get("run_id") or ""), str(params.get("reason") or ""))
        if method == "send_decision":
            return await self.send_decision(dict(params.get("decision") or {}))
        if method == "send_clarification":
            return await self.send_clarification(dict(params.get("clarification") or {}))
        if method == "finalize_work_order":
            return self.finalize_work_order(dict(params))
        if method == "continue_work_order":
            return await self.continue_work_order(str(params.get("work_order_id") or ""))
        if method == "pause_work_order":
            return self.pause_work_order(str(params.get("work_order_id") or ""), str(params.get("reason") or ""))
        if method == "finish_work_order":
            return self.finish_work_order(str(params.get("work_order_id") or ""), str(params.get("reason") or ""))
        if method == "shutdown":
            self._shutdown_event.set()
            return {"ok": True}
        raise ValueError(f"unknown minion manager method: {method}")

    def health(self) -> dict[str, Any]:
        active = [state for state in self.runs.values() if state.status in _ACTIVE_RUN_STATUSES]
        return {
            "ok": True,
            "started_at": self.started_at,
            "run_count": len(self.runs),
            "active_count": len(active),
            "pending_event_count": len(self.event_queue),
            "event_subscriber_count": len(self.event_subscribers),
            "log_path": str(minion_log_path(self.runtime_root)),
            **dict(self.endpoint_info),
        }

    def read_run(self, run_id: str) -> dict[str, Any]:
        state = self._require_run(run_id)
        detail = state.detail()
        detail["work_order_snapshot"] = self.tasking_repository.read_work_order(
            state.pack.work_order_id,
            active_runs=[item.summary() for item in self.runs.values()],
        )
        return detail

    async def spawn(self, pack_payload: dict[str, Any]) -> dict[str, Any]:
        pack = TaskContextPack.from_dict(pack_payload)
        if not pack.resolved_profile:
            pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(pack)
        pack = self.tasking_repository.prepare_pack_for_spawn(pack)
        if _is_plan_parent_pack(pack):
            continued = await self.continue_work_order(pack.work_order_id)
            return {
                "work_order_id": pack.work_order_id,
                "minion_profile": pack.minion_profile,
                "status": str(continued.get("status") or "ok"),
                "plan_parent": True,
                "continuation": continued,
            }
        minion_id = f"minion_{uuid4().hex[:10]}"
        run_id = f"run_{uuid4().hex[:12]}"
        metadata = dict(pack.metadata)
        metadata["run_id"] = run_id
        metadata["minion_id"] = minion_id
        pack = TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})
        pack = prepare_task_workspace(self.runtime_root, pack, run_id=run_id)
        self.tasking_repository.update_work_order_workspace(pack.work_order_id, dict(pack.workspace))
        pack = self._with_runner_debug_log(pack)
        state = MinionRunState(minion_id=minion_id, run_id=run_id, pack=pack)
        async with self._lock:
            self.runs[run_id] = state
        await self._start_runner(state)
        self._record_event(
            state,
            {
                "event_kind": "phase_started",
                "payload": {"phase": "spawned", "summary": "minion runner spawned"},
                "created_at": utc_now(),
            },
        )
        return state.summary()

    async def kill(self, run_id: str, reason: str = "") -> dict[str, Any]:
        state = self._require_run(run_id)
        process = state.process
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        state.status = "killed"
        state.ended_at = utc_now()
        self._record_event(
            state,
            {
                "event_kind": "terminal",
                "payload": {"status": "killed", "summary": reason or "minion killed"},
                "created_at": utc_now(),
            },
        )
        return state.summary()

    async def send_decision(self, payload: dict[str, Any]) -> dict[str, Any]:
        decision = MinionApprovalDecision.from_dict(payload)
        state = self._find_run_for_decision(decision)
        if state is None:
            raise KeyError(f"unknown approval target: {decision.approval_id}")
        if state.process is None or state.process.stdin is None or state.process.returncode is not None:
            raise RuntimeError(f"minion is not accepting decisions: {state.run_id}")
        state.process.stdin.write(pack_sidecar_message({"type": "decision", "decision": decision.to_dict()}))
        await state.process.stdin.drain()
        state.pending_approval = {}
        self._record_event(
            state,
            {
                "event_kind": "decision_received",
                "payload": decision.to_dict(),
                "created_at": utc_now(),
            },
        )
        return {"ok": True, "run": state.summary(), "decision": decision.to_dict()}

    async def send_clarification(self, payload: dict[str, Any]) -> dict[str, Any]:
        clarification_id = str(payload.get("clarification_id") or "").strip()
        state = self._find_run_for_clarification(payload)
        if state is None:
            raise KeyError(f"unknown clarification target: {clarification_id or payload.get('run_id') or ''}")
        if state.process is None or state.process.stdin is None or state.process.returncode is not None:
            raise RuntimeError(f"minion is not accepting clarifications: {state.run_id}")
        response = dict(payload)
        response.setdefault("run_id", state.run_id)
        response.setdefault("minion_id", state.minion_id)
        response.setdefault("work_order_id", state.pack.work_order_id)
        state.process.stdin.write(pack_sidecar_message({"type": "clarification", "clarification": response}))
        await state.process.stdin.drain()
        state.pending_clarification = {}
        state.status = "running"
        self._record_event(
            state,
            {
                "event_kind": "clarification_received",
                "payload": response,
                "created_at": utc_now(),
            },
        )
        for answer in list(response.get("answers") or []):
            if isinstance(answer, dict):
                with contextlib.suppress(Exception):
                    self.tasking_repository.record_clarification_answer(state.pack.work_order_id, dict(answer))
        return {"ok": True, "run": state.summary(), "clarification": response}

    def finalize_work_order(self, params: dict[str, Any]) -> dict[str, Any]:
        work_order_id = str(params.get("work_order_id") or "").strip()
        if not work_order_id:
            raise ValueError("work_order_id is required")
        snapshot = self.tasking_repository.read_work_order(work_order_id)
        if snapshot.get("status") == "not_found":
            raise KeyError(f"unknown work order: {work_order_id}")
        metadata = dict((snapshot.get("work_order") or {}).get("metadata") or {})
        workspace = dict(metadata.get("workspace") or {})
        repo_path = Path(str(params.get("repo_path") or workspace.get("repo_path") or ""))
        branch = str(params.get("work_order_branch") or workspace.get("work_order_branch") or "").strip()
        target = str(params.get("merge_target") or workspace.get("merge_target") or "").strip()
        message = str(params.get("message") or f"minion({work_order_id}): finalize work order")
        result = finalize_work_order_branch(repo_path, work_order_branch=branch, merge_target=target, message=message)
        event = {
            "event_kind": "finalized",
            "minion_id": "",
            "run_id": "",
            "work_order_id": work_order_id,
            "minion_profile": "",
            "payload": result,
            "created_at": utc_now(),
        }
        self._queue_event_delivery(event)
        self.tasking_repository.record_minion_event(event)
        return {"status": "ok" if result.get("status") in {"committed", "no_changes"} else "error", "work_order_id": work_order_id, **result}

    async def continue_work_order(self, work_order_id: str) -> dict[str, Any]:
        normalized = str(work_order_id or "").strip()
        if not normalized:
            raise ValueError("work_order_id is required")
        pack = self.tasking_repository.next_plan_module_pack(normalized, allow_paused=True)
        if pack is None:
            snapshot = self.tasking_repository.read_work_order(normalized)
            metadata = dict((snapshot.get("work_order") or {}).get("metadata") or {}) if snapshot.get("status") == "ok" else {}
            plan_execution = dict(metadata.get("plan_execution") or {})
            return {
                "status": str(plan_execution.get("status") or snapshot.get("status") or "not_available"),
                "work_order_id": normalized,
                "reason": "no_next_module",
            }
        if not pack.resolved_profile:
            pack = MinionProfileRegistry(runtime_root=self.runtime_root).resolve_pack(pack)
        run = await self.spawn(pack.to_dict())
        return {
            "status": "running_module",
            "work_order_id": normalized,
            "child_work_order_id": pack.work_order_id,
            "module_id": str(pack.metadata.get("module_id") or pack.metadata.get("parent_module_id") or ""),
            "run": run,
        }

    def pause_work_order(self, work_order_id: str, reason: str = "") -> dict[str, Any]:
        return self.tasking_repository.set_plan_parent_status(work_order_id, "paused", reason=reason)

    def finish_work_order(self, work_order_id: str, reason: str = "") -> dict[str, Any]:
        return self.tasking_repository.set_plan_parent_status(work_order_id, "completed", reason=reason)

    async def close_all(self) -> None:
        for state in list(self.runs.values()):
            if state.process is not None and state.process.returncode is None:
                with contextlib.suppress(Exception):
                    state.process.terminate()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(state.process.wait(), timeout=1.0)
                if state.process.returncode is None:
                    with contextlib.suppress(Exception):
                        state.process.kill()

    async def _start_runner(self, state: MinionRunState) -> None:
        log_dir = self.runtime_root / "data" / "minion" / "runs"
        log_dir.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pal.minion.runner_main",
            "--runtime-root",
            str(self.runtime_root),
            "--task-json",
            state.pack.to_json(),
            "--minion-id",
            state.minion_id,
            "--run-id",
            state.run_id,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=python_subprocess_env(),
        )
        state.process = process
        state.status = "running"
        state.stdout_task = asyncio.create_task(self._read_runner_stdout(state), name=f"minion-stdout-{state.run_id}")
        state.stderr_task = asyncio.create_task(self._read_runner_stderr(state), name=f"minion-stderr-{state.run_id}")
        state.wait_task = asyncio.create_task(self._wait_runner(state), name=f"minion-wait-{state.run_id}")

    def _with_runner_debug_log(self, pack: TaskContextPack) -> TaskContextPack:
        if not _debug_log_requested(pack):
            return pack
        log_path = minion_runner_log_path(self.runtime_root, pack.work_order_id, pack.minion_profile)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        metadata = dict(pack.metadata)
        metadata["debug_log"] = {
            "enabled": True,
            "path": str(log_path),
            "filename": log_path.name,
            "mode": "append",
            "managed_by": "minion.manager",
        }
        return TaskContextPack.from_dict({**pack.to_dict(), "metadata": metadata})

    async def _read_runner_stdout(self, state: MinionRunState) -> None:
        process = state.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                payload = await read_sidecar_message(process.stdout)
                if str(payload.get("type") or "") != "event":
                    continue
                self._record_event(state, payload)
        except asyncio.IncompleteReadError:
            return
        except Exception as exc:
            state.last_error = f"{exc.__class__.__name__}: {exc}"
            self.logger.exception("failed to read minion stdout: %s", state.run_id)

    async def _read_runner_stderr(self, state: MinionRunState) -> None:
        process = state.process
        if process is None or process.stderr is None:
            return
        try:
            while True:
                line = await process.stderr.readline()
                if not line:
                    return
                line_text = line.decode("utf-8", errors="replace").rstrip()
                self._record_runner_stderr_line(state, line_text)
                self.logger.info("minion %s stderr: %s", state.run_id, line_text)
        except Exception:
            return

    async def _wait_runner(self, state: MinionRunState) -> None:
        process = state.process
        if process is None:
            return
        returncode = await process.wait()
        await self._wait_for_stream_tasks(state)
        self._record_runner_exit(state, returncode)

    async def _wait_for_stream_tasks(self, state: MinionRunState) -> None:
        current = asyncio.current_task()
        tasks = [
            task
            for task in (state.stdout_task, state.stderr_task)
            if task is not None and task is not current and not task.done()
        ]
        if tasks:
            done, _pending = await asyncio.wait(tasks, timeout=1.0)
            for task in done:
                self._consume_task_result(state, task, "stream")

    def _record_runner_exit(self, state: MinionRunState, returncode: int | None) -> None:
        if state.status in _TERMINAL_RUN_STATUSES:
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
        self._record_event(
            state,
            {
                "event_kind": "terminal",
                "payload": payload,
                "created_at": utc_now(),
            },
        )

    def _reconcile_runs(self) -> None:
        for state in list(self.runs.values()):
            for task_name, task in (
                ("stdout", state.stdout_task),
                ("stderr", state.stderr_task),
                ("wait", state.wait_task),
            ):
                self._consume_task_result(state, task, task_name)
                if task is not None and task.done():
                    setattr(state, f"{task_name}_task", None)
            process = state.process
            returncode = getattr(process, "returncode", None) if process is not None else None
            if returncode is not None and state.status in _ACTIVE_RUN_STATUSES:
                self._record_runner_exit(state, returncode)

    def _consume_task_result(self, state: MinionRunState, task: asyncio.Task[None] | None, task_name: str) -> None:
        if task is None or not task.done():
            return
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            state.last_error = f"{exc.__class__.__name__}: {exc}"
            self.logger.exception("minion %s background %s task failed", state.run_id, task_name)
            if task_name == "wait" and state.status in _ACTIVE_RUN_STATUSES:
                self._record_event(
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

    def _record_runner_stderr_line(self, state: MinionRunState, line: str) -> None:
        normalized = str(line or "").strip()
        if not normalized:
            return
        state.stderr_tail.append(normalized)
        if len(state.stderr_tail) > 20:
            del state.stderr_tail[:-20]
        state.last_error = normalized

    def _record_event(self, state: MinionRunState, payload: dict[str, Any]) -> None:
        event_kind = str(payload.get("event_kind") or "")
        event = {
            "event_kind": event_kind,
            "minion_id": str(payload.get("minion_id") or state.minion_id),
            "run_id": str(payload.get("run_id") or state.run_id),
            "work_order_id": str(payload.get("work_order_id") or state.pack.work_order_id),
            "minion_profile": str(payload.get("minion_profile") or state.pack.minion_profile),
            "payload": dict(payload.get("payload") or {}),
            "created_at": str(payload.get("created_at") or utc_now()),
        }
        if event_kind == "approval_requested":
            state.status = "approval_pending"
            approval_payload = dict(event["payload"])
            approval_payload.setdefault("minion_id", state.minion_id)
            approval_payload.setdefault("run_id", state.run_id)
            approval_payload.setdefault("work_order_id", state.pack.work_order_id)
            metadata = dict(approval_payload.get("metadata") or {})
            if "control_route" not in metadata and isinstance(state.pack.metadata.get("control_route"), dict):
                metadata["control_route"] = dict(state.pack.metadata.get("control_route") or {})
            if metadata:
                approval_payload["metadata"] = metadata
            state.pending_approval = approval_payload
            event["payload"] = approval_payload
        elif event_kind == "clarification_requested":
            state.status = "clarification_pending"
            clarification_payload = dict(event["payload"])
            clarification_payload.setdefault("minion_id", state.minion_id)
            clarification_payload.setdefault("run_id", state.run_id)
            clarification_payload.setdefault("work_order_id", state.pack.work_order_id)
            metadata = dict(clarification_payload.get("metadata") or {})
            if "control_route" not in metadata and isinstance(state.pack.metadata.get("control_route"), dict):
                metadata["control_route"] = dict(state.pack.metadata.get("control_route") or {})
            if metadata:
                clarification_payload["metadata"] = metadata
            state.pending_clarification = clarification_payload
            event["payload"] = clarification_payload
        elif isinstance(state.pack.metadata.get("control_route"), dict):
            event_payload = dict(event["payload"])
            metadata = dict(event_payload.get("metadata") or {})
            metadata.setdefault("control_route", dict(state.pack.metadata.get("control_route") or {}))
            event_payload["metadata"] = metadata
            event["payload"] = event_payload
        if event_kind == "terminal":
            terminal_status = str(event["payload"].get("status") or "completed")
            state.status = terminal_status
            state.ended_at = utc_now()
            state.pending_approval = {}
            state.pending_clarification = {}
        if event_kind == "progress":
            self._update_progress_state(state, event)
        state.last_event = event
        state.last_event_at = str(event.get("created_at") or utc_now())
        state.ledger.append(event)
        self._queue_event_delivery(event)
        self.logger.info(
            "minion event run=%s kind=%s status=%s phase=%s summary=%s",
            state.run_id,
            event_kind,
            event["payload"].get("status", ""),
            event["payload"].get("phase", ""),
            str(event["payload"].get("summary") or "")[:240],
        )
        try:
            self.tasking_repository.record_minion_event(event)
        except Exception:
            self.logger.exception("failed to record minion tasking event: %s", state.run_id)
            return
        if event_kind == "terminal":
            self._schedule_serial_module_followup(state, event)

    def _schedule_serial_module_followup(self, state: MinionRunState, event: dict[str, Any]) -> None:
        payload = dict(event.get("payload") or {})
        if str(payload.get("status") or "").strip().lower() != "completed":
            return
        metadata = dict(state.pack.metadata or {})
        module_execution = dict(metadata.get("module_execution") or {})
        if str(module_execution.get("mode") or "") != "serial_module_milestones":
            return
        if not bool(module_execution.get("auto_advance")):
            return
        work_order_id = str(event.get("work_order_id") or state.pack.work_order_id)
        if not work_order_id or work_order_id in self._serial_followups_inflight:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._serial_followups_inflight.add(work_order_id)
        loop.create_task(self._continue_serial_module(work_order_id), name=f"minion-serial-followup-{work_order_id}")

    async def _continue_serial_module(self, work_order_id: str) -> None:
        try:
            next_pack = self.tasking_repository.next_serial_module_pack(work_order_id)
            if next_pack is not None:
                self.logger.info("minion serial module advancing work_order=%s", work_order_id)
                await self.spawn(next_pack.to_dict())
                return
            completion = self.tasking_repository.mark_serial_module_completed(work_order_id)
            if str(completion.get("status") or "") != "completed":
                return
            parent_completion = self.tasking_repository.record_plan_module_completion(work_order_id, completion)
            if str(parent_completion.get("status") or "") in {"awaiting_continue", "completed"}:
                event_work_order_id = str(parent_completion.get("parent_work_order_id") or work_order_id)
                event_payload = {**completion, **parent_completion}
            else:
                event_work_order_id = work_order_id
                event_payload = completion
            event = {
                "event_kind": "module_completed",
                "minion_id": "",
                "run_id": "",
                "work_order_id": event_work_order_id,
                "minion_profile": "software_engineering.coder",
                "payload": event_payload,
                "created_at": utc_now(),
            }
            self._queue_event_delivery(event)
            self.tasking_repository.record_minion_event(event)
            self.logger.info("minion serial module completed work_order=%s", work_order_id)
        except Exception:
            self.logger.exception("failed to continue serial minion module: %s", work_order_id)
        finally:
            self._serial_followups_inflight.discard(work_order_id)

    def _queue_event_delivery(self, event: dict[str, Any]) -> None:
        if not self.event_subscribers:
            self.event_queue.append(event)
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.event_queue.append(event)
            return
        loop.create_task(self._push_event_to_subscribers(dict(event)))

    async def _push_event_to_subscribers(self, event: dict[str, Any]) -> None:
        delivered = False
        for writer in list(self.event_subscribers):
            try:
                writer.write(pack_sidecar_message({"type": "event", "event": event}))
                await writer.drain()
                delivered = True
            except Exception:
                with contextlib.suppress(ValueError):
                    self.event_subscribers.remove(writer)
                with contextlib.suppress(Exception):
                    writer.close()
        if not delivered:
            self.event_queue.append(event)

    async def _close_event_subscribers(self) -> None:
        for writer in list(self.event_subscribers):
            with contextlib.suppress(Exception):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), timeout=0.2)
        self.event_subscribers.clear()

    def _update_progress_state(self, state: MinionRunState, event: dict[str, Any]) -> None:
        payload = dict(event.get("payload") or {})
        phase = str(payload.get("phase") or "").strip()
        if phase:
            state.last_phase = phase
        round_value = payload.get("round")
        if phase in {"llm_round_started", "llm_round_completed"}:
            with contextlib.suppress(TypeError, ValueError):
                state.llm_round_count = max(state.llm_round_count, int(round_value or 0))
        if phase in {"tool_call_started", "tool_call_completed", "tool_call_failed"}:
            state.last_tool_call = {
                "phase": phase,
                "tool_name": str(payload.get("tool_name") or ""),
                "target_name": str(payload.get("target_name") or ""),
                "round": payload.get("round"),
                "tool_call_index": payload.get("tool_call_index"),
                "ok": payload.get("ok"),
                "status": payload.get("status"),
                "error_type": payload.get("error_type"),
            }
            if phase == "tool_call_completed":
                state.tool_call_count += 1

    def _require_run(self, run_id: str) -> MinionRunState:
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(f"unknown minion run: {run_id}")
        return state

    def _find_run_for_decision(self, decision: MinionApprovalDecision) -> MinionRunState | None:
        if decision.run_id and decision.run_id in self.runs:
            return self.runs[decision.run_id]
        for state in self.runs.values():
            if str(state.pending_approval.get("approval_id") or "") == decision.approval_id:
                return state
        return None

    def _find_run_for_clarification(self, payload: dict[str, Any]) -> MinionRunState | None:
        run_id = str(payload.get("run_id") or "").strip()
        clarification_id = str(payload.get("clarification_id") or "").strip()
        if run_id and run_id in self.runs:
            return self.runs[run_id]
        for state in self.runs.values():
            if clarification_id and str(state.pending_clarification.get("clarification_id") or "") == clarification_id:
                return state
        return None


def _debug_log_requested(pack: TaskContextPack) -> bool:
    metadata = dict(pack.metadata or {})
    debug_log = metadata.get("debug_log")
    if isinstance(debug_log, dict) and "enabled" in debug_log:
        return bool(debug_log.get("enabled"))
    return bool(metadata.get("minion_debug_log_enabled") or metadata.get("prompt_log_enabled"))


def _is_plan_parent_pack(pack: TaskContextPack) -> bool:
    metadata = dict(pack.metadata or {})
    plan_execution = dict(metadata.get("plan_execution") or {})
    return str(plan_execution.get("mode") or "") == "module_parent_milestones"


def _debug_log_path_from_pack(pack: TaskContextPack) -> str:
    debug_log = dict((pack.metadata or {}).get("debug_log") or {})
    if not bool(debug_log.get("enabled")):
        return ""
    return str(debug_log.get("path") or "")

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.foundation import utc_now
from pal.minion.ipc import python_subprocess_env
from pal.minion.utils import safe_token


STEP_PROCESS_PAYLOAD_DIR = Path("data") / "minion" / "step_payloads"
STEP_PROCESS_TERMINAL_STATUSES = {"completed", "failed", "timeout", "killed"}
STEP_PROCESS_TRANSITIONS = {
    "pending": {"running", "failed", "killed"},
    "running": STEP_PROCESS_TERMINAL_STATUSES,
    "completed": set(),
    "failed": set(),
    "timeout": set(),
    "killed": set(),
}


@dataclass
class StepProcessState:
    step_id: str
    parent_run_id: str
    payload_path: Path
    status: str = "pending"
    process: asyncio.subprocess.Process | None = None
    started_at: str = field(default_factory=utc_now)
    ended_at: str = ""
    returncode: int | None = None
    timed_out: bool = False
    result: dict[str, Any] = field(default_factory=dict)
    stdout: bytes = b""
    stderr: bytes = b""

    def summary(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "parent_run_id": self.parent_run_id,
            "status": self.status,
            "pid": self.process.pid if self.process is not None else None,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "payload_path": str(self.payload_path),
            "stderr_tail": _stderr_tail(self.stderr),
        }


@dataclass
class StepProcessSupervisor:
    manager: Any
    states: dict[str, StepProcessState] = field(default_factory=dict)

    async def run_once(
        self,
        payload: dict[str, Any],
        *,
        step_id: str = "",
        timeout_seconds: float | None = None,
        supervisor_grace_seconds: float = 5.0,
    ) -> dict[str, Any]:
        normalized_payload = dict(payload or {})
        parent_run_id = str(normalized_payload.get("parent_run_id") or step_id or f"step_run_{uuid4().hex[:12]}").strip()
        normalized_step_id = str(step_id or parent_run_id).strip() or parent_run_id
        normalized_payload["parent_run_id"] = parent_run_id
        path = self.write_step_payload(parent_run_id, normalized_payload)
        state = StepProcessState(step_id=normalized_step_id, parent_run_id=parent_run_id, payload_path=path)
        self.states[state.step_id] = state
        try:
            argv = [
                sys.executable,
                "-m",
                "pal.minion.step_process_main",
                "--runtime-root",
                str(self.manager.runtime_root),
                "--step-json-file",
                str(path),
            ]
            child_timeout = max(0.0, float(timeout_seconds or 0.0))
            if child_timeout > 0:
                argv.extend(["--timeout-seconds", str(child_timeout)])
            state.process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=python_subprocess_env(),
            )
            self._transition(state, "running")
            supervisor_timeout = None
            if child_timeout > 0:
                supervisor_timeout = child_timeout + max(0.001, float(supervisor_grace_seconds or 0.0))
            try:
                if supervisor_timeout is None:
                    state.stdout, state.stderr = await state.process.communicate()
                else:
                    state.stdout, state.stderr = await asyncio.wait_for(
                        state.process.communicate(),
                        timeout=supervisor_timeout,
                    )
            except asyncio.TimeoutError:
                await self._terminate_state(state, status="killed")
                if state.process is not None:
                    state.stdout, state.stderr = await state.process.communicate()
                state.result = {
                    "status": "failed",
                    "reason": "step_process_unresponsive_after_timeout",
                    "timeout_seconds": child_timeout,
                    "supervisor_grace_seconds": supervisor_grace_seconds,
                    "process": state.summary(),
                }
                return dict(state.result)
            state.returncode = state.process.returncode if state.process is not None else None
            result = _parse_step_stdout(state.stdout)
            if state.returncode not in (0, None) and str(result.get("status") or "") != "failed":
                result = {**dict(result), "status": "failed", "reason": "step_process_nonzero_exit"}
            terminal_status = "completed" if state.returncode == 0 and str(result.get("status") or "") != "failed" else "failed"
            if str(result.get("reason") or "") == "step_process_timeout":
                state.timed_out = True
                terminal_status = "timeout"
            self._transition(state, terminal_status)
            state.result = dict(result)
            state.result["process"] = state.summary()
            return dict(state.result)
        finally:
            await self._cleanup_state(state, reason="step_process_exit")
            self.states.pop(state.step_id, None)

    async def close_all(self) -> None:
        for state in list(self.states.values()):
            if state.status not in STEP_PROCESS_TERMINAL_STATUSES:
                await self._terminate_state(state, status="killed")
            await self._cleanup_state(state, reason="manager_shutdown")

    def write_step_payload(self, run_id: str, payload: dict[str, Any]) -> Path:
        path = step_process_payload_path(self.manager.runtime_root, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(payload), sort_keys=True), encoding="utf-8")
        return path

    async def _terminate_state(self, state: StepProcessState, *, status: str) -> None:
        process = state.process
        if process is None:
            self._transition(state, status)
            return
        if process.returncode is not None:
            state.returncode = process.returncode
            self._transition(state, status)
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
        state.returncode = process.returncode
        self._transition(state, status)

    async def _cleanup_state(self, state: StepProcessState, *, reason: str) -> None:
        if state.process is not None and state.process.returncode is None:
            await self._terminate_state(state, status="killed")
        released = self.manager._release_logical_slots_for_run(state.parent_run_id, reason=reason)
        with contextlib.suppress(OSError):
            state.payload_path.unlink()
        if released:
            self.manager.logger.info("released %d step process logical slots for %s", len(released), state.parent_run_id)

    def _transition(self, state: StepProcessState, status: str) -> None:
        normalized = str(status or "").strip()
        if normalized not in STEP_PROCESS_TRANSITIONS:
            raise ValueError(f"unknown step process status: {normalized}")
        if normalized == state.status:
            return
        allowed = STEP_PROCESS_TRANSITIONS.get(state.status, set())
        if normalized not in allowed:
            raise ValueError(f"invalid step process transition: {state.status} -> {normalized}")
        state.status = normalized
        if normalized in STEP_PROCESS_TERMINAL_STATUSES:
            state.ended_at = utc_now()


def step_process_payload_path(runtime_root: Path, run_id: str) -> Path:
    return Path(runtime_root) / STEP_PROCESS_PAYLOAD_DIR / f"{safe_token(run_id, limit=120)}.json"


def _parse_step_stdout(stdout: bytes) -> dict[str, Any]:
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return {"status": "failed", "reason": "step_process_empty_stdout"}
    line = text.splitlines()[-1]
    try:
        payload = json.loads(line)
    except json.JSONDecodeError as exc:
        return {"status": "failed", "reason": "step_process_invalid_stdout", "error": f"{exc.__class__.__name__}: {exc}"}
    if not isinstance(payload, dict):
        return {"status": "failed", "reason": "step_process_stdout_not_object"}
    return dict(payload)


def _stderr_tail(stderr: bytes, *, limit: int = 20) -> list[str]:
    text = stderr.decode("utf-8", errors="replace")
    return [line.rstrip() for line in text.splitlines()[-limit:] if line.rstrip()]

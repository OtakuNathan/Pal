from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from collections import deque
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from pal.foundation import utc_now
from pal.llm.contracts import CanonicalToolCall
from pal.minion.harness_request import compile_architect_harness_request
from pal.minion.ipc import ROLE_GATEWAY_TOKEN_ENV, MinionRoleGatewayClient
from pal.minion.v2.contract_submission import contract_submit_tool_result
from pal.minion.v2.work_items import (
    read_work_items,
    update_checklist_tool_result,
)
from pal.shared import MinionInvocationPack


class CodexHarnessError(RuntimeError):
    pass


class CodexAppServer:
    def __init__(
        self,
        *,
        codex_bin: str,
        cwd: Path,
        effort: str,
        timeout_seconds: float,
    ) -> None:
        self.codex_bin = str(codex_bin)
        self.cwd = Path(cwd)
        self.effort = str(effort or "high")
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.process: asyncio.subprocess.Process | None = None
        self._sequence = 0
        self._pending: deque[dict[str, Any]] = deque()
        self._stderr = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "CodexAppServer":
        self.process = await asyncio.create_subprocess_exec(
            self.codex_bin,
            "app-server",
            "-c",
            f'model_reasoning_effort="{self.effort}"',
            "--listen",
            "stdio://",
            cwd=str(self.cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=False,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        response = await self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "pal-codex-architect-harness",
                    "title": "Pal Codex Architect Harness",
                    "version": "0.1.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        if response.get("error"):
            raise CodexHarnessError(str(response["error"]))
        await self.notify("initialized", {})
        return self

    async def __aexit__(self, *_args: object) -> None:
        process = self.process
        self.process = None
        if process is not None and process.returncode is None:
            process.terminate()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=3.0)
            if process.returncode is None:
                process.kill()
                await process.wait()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._stderr_task

    async def request(
        self,
        method: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._sequence += 1
        request_id = self._sequence
        await self.write(
            {"id": request_id, "method": method, "params": dict(params)}
        )
        while True:
            message = await self._read_wire()
            if message.get("id") == request_id:
                return message
            if "id" in message and message.get("method"):
                await self.error_response(
                    message["id"],
                    -32601,
                    f"unsupported server request during {method}",
                )
                continue
            self._pending.append(message)

    async def notify(
        self,
        method: str,
        params: Mapping[str, Any],
    ) -> None:
        await self.write({"method": method, "params": dict(params)})

    async def write(self, message: Mapping[str, Any]) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise CodexHarnessError("Codex app-server stdin is unavailable")
        process.stdin.write(
            (
                json.dumps(dict(message), ensure_ascii=False)
                + "\n"
            ).encode("utf-8")
        )
        await process.stdin.drain()

    async def result_response(
        self,
        request_id: Any,
        result: Mapping[str, Any],
    ) -> None:
        await self.write({"id": request_id, "result": dict(result)})

    async def error_response(
        self,
        request_id: Any,
        code: int,
        message: str,
    ) -> None:
        await self.write(
            {
                "id": request_id,
                "error": {"code": int(code), "message": str(message)},
            }
        )

    async def read(self) -> dict[str, Any]:
        if self._pending:
            return self._pending.popleft()
        return await self._read_wire()

    async def _read_wire(self) -> dict[str, Any]:
        process = self._require_process()
        if process.stdout is None:
            raise CodexHarnessError("Codex app-server stdout is unavailable")
        try:
            raw = await asyncio.wait_for(
                process.stdout.readline(),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise CodexHarnessError(
                "Codex app-server turn reached its idle timeout"
            ) from exc
        if not raw:
            tail = self._stderr.decode("utf-8", errors="replace")[-4000:]
            raise CodexHarnessError(
                "Codex app-server exited before completing the turn"
                + (f": {tail}" if tail else "")
            )
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise CodexHarnessError(
                f"Codex app-server emitted invalid JSON: {raw[:500]!r}"
            ) from exc
        if not isinstance(value, dict):
            raise CodexHarnessError("Codex app-server message is not an object")
        if value.get("error") and "id" not in value:
            raise CodexHarnessError(str(value["error"]))
        return value

    async def _drain_stderr(self) -> None:
        process = self._require_process()
        if process.stderr is None:
            return
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                return
            self._stderr.extend(chunk)
            if len(self._stderr) > 64 * 1024:
                del self._stderr[: len(self._stderr) - 64 * 1024]

    def _require_process(self) -> asyncio.subprocess.Process:
        if self.process is None:
            raise CodexHarnessError("Codex app-server is not running")
        return self.process


class CodexArchitectWorker:
    def __init__(
        self,
        *,
        runtime_root: Path,
        pack: MinionInvocationPack,
        minion_id: str,
        run_id: str,
    ) -> None:
        self.runtime_root = Path(runtime_root)
        self.pack = pack
        self.minion_id = str(minion_id)
        self.run_id = str(run_id)
        self.request = compile_architect_harness_request(pack)
        token = str(os.environ.get(ROLE_GATEWAY_TOKEN_ENV) or "").strip()
        self.gateway = MinionRoleGatewayClient(self.runtime_root, token)
        binding = dict(dict(pack.metadata or {}).get("minion_v2") or {})
        self.workflow_id = str(binding.get("workflow_id") or "")
        self.harness_config = dict(binding.get("harness_config") or {})
        self.workspace = {
            **dict(pack.workspace or {}),
            "runtime_root": str(self.runtime_root),
            "minion_v2": binding,
        }
        self.controls: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.plan_revision = 0

    async def run(self) -> int:
        stdin_task = asyncio.create_task(self._read_stdin())
        try:
            await self._initialize_work_items()
            await self.emit(
                "progress",
                {
                    "phase": "harness_start",
                    "summary": "Codex Architect harness started.",
                },
            )
            codex_bin = str(
                self.harness_config.get("codex_bin") or "codex"
            )
            effort = str(self.harness_config.get("effort") or "high")
            timeout = float(
                self.harness_config.get("turn_timeout_seconds") or 3000
            )
            async with CodexAppServer(
                codex_bin=codex_bin,
                cwd=self.request.cwd,
                effort=effort,
                timeout_seconds=timeout,
            ) as server:
                thread_id = await self._open_thread(server)
                correction = self.request.user_input
                for correction_index in range(4):
                    turn_id = await self._start_turn(
                        server,
                        thread_id=thread_id,
                        text=correction,
                        effort=effort,
                    )
                    plan_error = await self._drive_turn(
                        server,
                        thread_id=thread_id,
                        turn_id=turn_id,
                    )
                    submission = contract_submit_tool_result(
                        CanonicalToolCall(
                            name="contract_submit",
                            args={},
                            call_id=(
                                f"codex-submit-{turn_id}-"
                                f"{correction_index}"
                            ),
                        ),
                        self.workspace,
                    )
                    if submission.ok:
                        await self.emit(
                            "terminal",
                            {
                                "status": "completed",
                                "summary": (
                                    "Codex Architect output passed Manager "
                                    "validation."
                                ),
                                "submission": dict(
                                    submission.structured or {}
                                ),
                            },
                        )
                        return 0
                    if correction_index >= 3:
                        raise CodexHarnessError(submission.llm_text)
                    correction = (
                        "Manager validation rejected the current output. "
                        "Correct only the reported defect in the existing "
                        "files and checklist, then finish again.\n\n"
                        + (
                            f"Native plan projection error: {plan_error}\n\n"
                            if plan_error
                            else ""
                        )
                        + str(submission.llm_text or submission.text)
                    )
                    await self.emit(
                        "progress",
                        {
                            "phase": "validation_repair",
                            "summary": str(
                                submission.text
                                or "Manager requested a bounded repair."
                            ),
                        },
                    )
        finally:
            stdin_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stdin_task

    async def _open_thread(self, server: CodexAppServer) -> str:
        continuation = await asyncio.to_thread(
            self.gateway.request_sync,
            "harness_state_read",
            {},
        )
        state = dict(continuation.get("state") or {})
        thread_id = str(state.get("thread_id") or "").strip()
        common: dict[str, Any] = {
            "cwd": str(self.request.cwd),
            "developerInstructions": self.request.developer_instructions,
            "approvalPolicy": "never",
            "sandbox": "workspace-write",
        }
        model = str(self.harness_config.get("model") or "").strip()
        if model:
            common["model"] = model
        if thread_id:
            response = await server.request(
                "thread/resume",
                {"threadId": thread_id, **common},
            )
        else:
            response = await server.request(
                "thread/start",
                {"ephemeral": False, **common},
            )
        if response.get("error"):
            raise CodexHarnessError(str(response["error"]))
        resolved = str(
            dict(dict(response.get("result") or {}).get("thread") or {}).get(
                "id"
            )
            or thread_id
        ).strip()
        if not resolved:
            raise CodexHarnessError("Codex thread response contained no id")
        await asyncio.to_thread(
            self.gateway.request_sync,
            "harness_state_write",
            {"state": {"thread_id": resolved}},
        )
        return resolved

    async def _start_turn(
        self,
        server: CodexAppServer,
        *,
        thread_id: str,
        text: str,
        effort: str,
    ) -> str:
        response = await server.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": str(text)}],
                "cwd": str(self.request.cwd),
                "effort": effort,
                "approvalPolicy": "never",
                "sandboxPolicy": {
                    "type": "workspaceWrite",
                    "writableRoots": [str(self.request.cwd)],
                    "networkAccess": False,
                },
            },
        )
        if response.get("error"):
            raise CodexHarnessError(str(response["error"]))
        turn_id = str(
            dict(dict(response.get("result") or {}).get("turn") or {}).get(
                "id"
            )
            or ""
        )
        if not turn_id:
            raise CodexHarnessError("Codex turn response contained no id")
        return turn_id

    async def _drive_turn(
        self,
        server: CodexAppServer,
        *,
        thread_id: str,
        turn_id: str,
    ) -> str:
        plan_error = ""
        while True:
            message = await server.read()
            method = str(message.get("method") or "")
            params = dict(message.get("params") or {})
            message_thread = str(params.get("threadId") or "")
            message_turn = str(params.get("turnId") or "")
            if message_thread and message_thread != thread_id:
                continue
            if message_turn and message_turn != turn_id:
                continue
            if method == "turn/plan/updated":
                try:
                    await self._update_plan(
                        list(params.get("plan") or []),
                        turn_id=turn_id,
                    )
                    plan_error = ""
                except Exception as exc:
                    plan_error = f"{exc.__class__.__name__}: {exc}"
                continue
            if method == "item/tool/requestUserInput" and "id" in message:
                await self._handle_question(server, message, params)
                continue
            if "id" in message and method:
                await server.error_response(
                    message["id"],
                    -32601,
                    "This Architect harness exposes no external tool request.",
                )
                continue
            if method == "turn/completed":
                status = str(
                    dict(params.get("turn") or {}).get("status")
                    or params.get("status")
                    or "completed"
                )
                if status != "completed":
                    raise CodexHarnessError(
                        f"Codex turn ended with status {status}"
                    )
                return plan_error

    async def _handle_question(
        self,
        server: CodexAppServer,
        message: Mapping[str, Any],
        params: Mapping[str, Any],
    ) -> None:
        questions = [
            dict(item)
            for item in list(params.get("questions") or [])
            if isinstance(item, Mapping)
        ]
        if len(questions) != 1:
            await server.error_response(
                message["id"],
                -32602,
                "Ask exactly one decisive user question at a time.",
            )
            return
        question = questions[0]
        options = [
            dict(item)
            for item in list(question.get("options") or [])
            if isinstance(item, Mapping)
        ]
        if len(options) < 2 or len(options) > 3:
            await server.error_response(
                message["id"],
                -32602,
                "The question must provide two or three concrete options.",
            )
            return
        clarification_id = f"clarify_{uuid4().hex[:16]}"
        await self.emit(
            "clarification_requested",
            {
                "clarification_id": clarification_id,
                "run_id": self.run_id,
                "minion_id": self.minion_id,
                "invocation_id": self.pack.invocation_id,
                "workflow_id": self.workflow_id,
                "title": str(
                    question.get("header") or "Architecture question"
                ),
                "questions": [
                    {
                        "id": str(question.get("id") or "architecture-question"),
                        "title": str(
                            question.get("header")
                            or "Architecture question"
                        ),
                        "question": str(question.get("question") or ""),
                        "options": options,
                    }
                ],
                "status": "pending",
            },
        )
        control = await self.controls.get()
        clarification = dict(control.get("clarification") or {})
        if (
            str(control.get("type") or "") != "clarification"
            or str(clarification.get("clarification_id") or "")
            != clarification_id
        ):
            raise CodexHarnessError(
                "Manager clarification response did not match the question"
            )
        answers = [
            dict(item)
            for item in list(clarification.get("answers") or [])
            if isinstance(item, Mapping)
        ]
        answer = str(
            (answers[0] if answers else {}).get("answer") or ""
        ).strip()
        if not answer:
            raise CodexHarnessError("user clarification contained no answer")
        question_id = str(
            question.get("id") or "architecture-question"
        )
        await server.result_response(
            message["id"],
            {"answers": {question_id: {"answers": [answer]}}},
        )
        await self.emit(
            "clarification_received",
            {
                "clarification_id": clarification_id,
                "answer_count": 1,
            },
        )

    async def _initialize_work_items(self) -> None:
        existing = await asyncio.to_thread(
            read_work_items,
            self.workspace,
        )
        if list(existing.get("items") or []):
            return
        seed = [
            dict(item) for item in self.request.work_item_seed
        ]
        plan = [
            {
                "step": str(item.get("summary") or ""),
                "status": (
                    "in_progress" if index == 0 else "pending"
                ),
            }
            for index, item in enumerate(seed)
            if str(item.get("summary") or "").strip()
        ]
        if plan:
            await self._write_checklist(
                plan,
                operation_key="codex-initialize-checklist",
            )

    async def _update_plan(
        self,
        raw_plan: list[Any],
        *,
        turn_id: str,
    ) -> None:
        status_map = {
            "pending": "pending",
            "inProgress": "in_progress",
            "completed": "completed",
        }
        native = [
            {
                "step": str(dict(item).get("step") or "").strip(),
                "status": status_map.get(
                    str(dict(item).get("status") or ""),
                    "pending",
                ),
            }
            for item in raw_plan
            if isinstance(item, Mapping)
            and str(dict(item).get("step") or "").strip()
        ]
        seed = [
            dict(item) for item in self.request.work_item_seed
        ]
        fixed = [
            str(item.get("summary") or "")
            for item in seed
            if str(item.get("kind") or "") == "phase"
        ]
        required = [
            str(item.get("summary") or "")
            for item in seed
            if bool(item.get("required", True))
        ]
        by_step = {str(item["step"]): dict(item) for item in native}
        plan = [
            by_step.get(
                step,
                {"step": step, "status": "pending"},
            )
            for step in fixed
        ]
        plan.extend(
            item for item in native if str(item["step"]) not in set(fixed)
        )
        present = {str(item["step"]) for item in plan}
        plan.extend(
            {"step": step, "status": "pending"}
            for step in required
            if step not in present
        )
        self.plan_revision += 1
        await self._write_checklist(
            plan,
            operation_key=(
                f"codex-plan-{turn_id}-{self.plan_revision}"
            ),
        )

    async def _write_checklist(
        self,
        plan: list[dict[str, str]],
        *,
        operation_key: str,
    ) -> None:
        result = await asyncio.to_thread(
            update_checklist_tool_result,
            CanonicalToolCall(
                name="update_checklist",
                args={"plan": plan},
                call_id=operation_key,
            ),
            self.workspace,
        )
        if not result.ok:
            raise CodexHarnessError(result.llm_text or result.text)

    async def _read_stdin(self) -> None:
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        loop = asyncio.get_running_loop()
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        while True:
            raw = await reader.readline()
            if not raw:
                return
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                await self.controls.put(value)

    async def emit(
        self,
        event_kind: str,
        payload: Mapping[str, Any],
    ) -> None:
        event = {
            "type": "event",
            "event_kind": str(event_kind),
            "minion_id": self.minion_id,
            "run_id": self.run_id,
            "invocation_id": self.pack.invocation_id,
            "workflow_id": self.workflow_id,
            "minion_profile": self.pack.minion_profile,
            "payload": dict(payload),
            "created_at": utc_now(),
        }
        print(
            json.dumps(
                {"kind": "event", "event": event},
                ensure_ascii=False,
            ),
            flush=True,
        )


async def _run(args: argparse.Namespace) -> int:
    payload = json.loads(
        Path(args.pack_json).read_text(encoding="utf-8")
    )
    worker = CodexArchitectWorker(
        runtime_root=Path(args.runtime_root),
        pack=MinionInvocationPack.from_dict(dict(payload)),
        minion_id=str(args.minion_id),
        run_id=str(args.run_id),
    )
    return await worker.run()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--pack-json", required=True)
    parser.add_argument("--minion-id", required=True)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        return asyncio.run(_run(args))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "kind": "worker_error",
                    "error": f"{exc.__class__.__name__}: {exc}",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pal.execution.contracts import CapabilityResult
from pal.shared import (
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
)


SHELL_EXEC_DESCRIPTION = (
    "Run shell; returns stdout, stderr, and exit status. "
    "Use for tests, builds, scripts, process probes, and package commands. "
    "Pal runtime, module, capability, minion state: use search_tools/read_tool/call_tool or the visible Pal tool before shell. "
    "When visible, use op_tree for structured directory listings; "
    "op_search for repository text search; op_file_read for reading text files; op_file_edit for precise in-place edits after reading; "
    "op_file_write for creating, overwriting, or appending UTF-8 text files; op_path_delete for deleting files or directories; "
    "op_git for git status, diff, log, show, and audited git restore/revert. "
    "Do not use shell commands such as cat/head/tail for file inspection, grep/rg for repository search, sed/awk for edits, "
    "tee/echo/printf redirection for writes, or rm/unlink/rmdir/git rm/find -delete for deletion when the matching capability is visible. "
    "Piping command output through head/tail to shorten stdout or stderr is fine. "
    "In minion workspaces, do not use shell for git add/commit or other checkpoint mutations; use the dedicated checkpoint commit capability instead."
)

SHELL_EXEC_CMD_DESCRIPTION = (
    "Shell command to execute. Use only for command execution, tests, builds, scripts, process probes, and package commands. "
    "If visible, prefer op_tree for listings, op_search for text search, op_file_read for file reads, op_file_edit for edits, "
    "op_file_write for writes, op_path_delete for deletion, and op_git for git status/diff/log/show. "
    "Avoid cat/head/tail/grep/rg/sed/awk/tee/echo/printf redirection/rm/unlink/rmdir/git rm/find -delete for repo file operations when the matching capability is visible. "
    "For Pal runtime/module/minion/capability state or actions, use built-in Pal tools before shell. "
    "In minion workspaces, do not run git add/commit/reset/checkout/clean/merge/rebase/push for checkpointing; use the dedicated checkpoint commit capability instead."
)

@dataclass(unsafe_hash=True)
class _TrackedShellProcess:
    proc: asyncio.subprocess.Process
    _cancel_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False, compare=False, hash=False)

    async def cancel(self) -> None:
        if self.proc.returncode is not None:
            return
        cancel_task = self._cancel_task
        if cancel_task is None or cancel_task.done():
            cancel_task = asyncio.create_task(self._cancel_once())
            self._cancel_task = cancel_task
        try:
            await cancel_task
        finally:
            if self._cancel_task is cancel_task and cancel_task.done():
                self._cancel_task = None

    async def _cancel_once(self) -> None:
        if self.proc.returncode is not None:
            return
        if os.name == "nt":
            await self._cancel_windows()
            return
        await self._cancel_posix()

    async def _cancel_posix(self) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=1.0)
            return
        except asyncio.TimeoutError:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            await self.proc.wait()

    async def _cancel_windows(self) -> None:
        if hasattr(signal, "CTRL_BREAK_EVENT"):
            with contextlib.suppress(Exception):
                self.proc.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=1.0)
                return
            except asyncio.TimeoutError:
                pass
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(self.proc.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        with contextlib.suppress(Exception):
            await killer.wait()
        with contextlib.suppress(Exception):
            await self.proc.wait()


@dataclass
class ShellExecTool:
    name: str = "shell_exec"
    display_name: str = "Shell Exec"
    family: str = "system"
    description: str = SHELL_EXEC_DESCRIPTION
    tags: tuple[str, ...] = ("shell", "system", "exec")
    keywords: tuple[str, ...] = ("command", "terminal", "bash", "zsh", "pwsh", "powershell", "run")
    args_schema: dict[str, object] = None  # type: ignore[assignment]   
    result_schema: dict[str, object] = None  # type: ignore[assignment]
    default_timeout_ms: int = 30_000
    shell_path: str = ""

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": SHELL_EXEC_CMD_DESCRIPTION},
                    "cwd": {"type": "string", "description": "Optional working directory."},
                    "timeout_ms": {"type": "integer", "minimum": 1, "description": "Optional timeout in milliseconds."},
                },
                "required": ["cmd"],
            }
        if self.result_schema is None:
            self.result_schema = {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "cwd": {"type": "string"},
                    "returncode": {"type": "integer"},
                    "stdout": {"type": "string"},
                    "stderr": {"type": "string"},
                    "stdout_truncated": {"type": "boolean"},
                    "stderr_truncated": {"type": "boolean"},
                    "timeout_ms": {"type": "integer"},
                },
            }
        if not self.shell_path:
            self.shell_path = self._default_shell_path()

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        cmd = str(args.get("cmd") or "").strip()
        if not cmd:
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="cmd is required",
                structured={"reason": "missing_cmd"},
                llm_text="cmd is required",
            )
        cwd_value = args.get("cwd")
        cwd = str(cwd_value).strip() if isinstance(cwd_value, str) and cwd_value.strip() else None
        timeout_ms = self._coerce_int(args.get("timeout_ms"), default=self.default_timeout_ms, minimum=1)

        completed = subprocess.run(
            self._build_shell_command(cmd),
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout_ms / 1000.0,
            check=False,
        )

        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        ok = completed.returncode == 0
        display_text = (stdout if ok else stderr or stdout).strip()
        if not display_text:
            display_text = f"command exited with code {completed.returncode}"

        return CapabilityResult(
            status=RuntimeStatus.OK if ok else RuntimeStatus.ERROR,
            text=display_text,
            structured={
                "cmd": cmd,
                "cwd": cwd or str(Path.cwd()),
                "display_text": display_text,
                "returncode": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "timeout_ms": timeout_ms,
            },
            llm_text=display_text,
        )

    async def ainvoke(self, args: dict[str, object], **kwargs: object) -> CapabilityResult:
        runtime = kwargs.get("runtime")
        turn_id = str(kwargs.get("turn_id") or "").strip() or None
        cmd = str(args.get("cmd") or "").strip()
        if not cmd:
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="cmd is required",
                structured={"reason": "missing_cmd"},
                llm_text="cmd is required",
            )
        cwd_value = args.get("cwd")
        cwd = str(cwd_value).strip() if isinstance(cwd_value, str) and cwd_value.strip() else None
        timeout_ms = self._coerce_int(args.get("timeout_ms"), default=self.default_timeout_ms, minimum=1)
        proc = await self._create_async_process(cmd, cwd=cwd)
        tracked = _TrackedShellProcess(proc=proc)
        register = getattr(runtime, "register_interrupt_handle", None)
        if callable(register):
            register(turn_id, tracked)
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_ms / 1000.0)
        except asyncio.TimeoutError:
            await tracked.cancel()
            return CapabilityResult(
                status=RuntimeStatus.ERROR,
                text="command timed out",
                structured={
                    "cmd": cmd,
                    "cwd": cwd or str(Path.cwd()),
                    "display_text": "command timed out",
                    "returncode": None,
                    "stdout": "",
                    "stderr": "",
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "timeout_ms": timeout_ms,
                    "timed_out": True,
                },
                llm_text="command timed out",
            )
        finally:
            release = getattr(runtime, "release_interrupt_handle", None)
            if callable(release):
                release(turn_id, tracked)
        stdout = stdout_bytes.decode("utf-8", errors="replace") if isinstance(stdout_bytes, (bytes, bytearray)) else str(stdout_bytes or "")
        stderr = stderr_bytes.decode("utf-8", errors="replace") if isinstance(stderr_bytes, (bytes, bytearray)) else str(stderr_bytes or "")
        ok = proc.returncode == 0
        display_text = (stdout if ok else stderr or stdout).strip()
        if not display_text:
            display_text = f"command exited with code {proc.returncode}"
        return CapabilityResult(
            status=RuntimeStatus.OK if ok else RuntimeStatus.ERROR,
            text=display_text,
            structured={
                "cmd": cmd,
                "cwd": cwd or str(Path.cwd()),
                "display_text": display_text,
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "stdout_truncated": False,
                "stderr_truncated": False,
                "timeout_ms": timeout_ms,
            },
            llm_text=display_text,
        )

    async def _create_async_process(self, cmd: str, *, cwd: str | None) -> asyncio.subprocess.Process:
        kwargs: dict[str, object] = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": cwd,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["preexec_fn"] = os.setsid
        return await asyncio.create_subprocess_exec(
            *self._build_shell_command(cmd),
            **kwargs,
        )

    def _build_shell_command(self, cmd: str) -> list[str]:
        shell = self.shell_path or self._default_shell_path()
        shell_name = Path(shell).name.lower()
        if shell_name in {"pwsh", "pwsh.exe", "powershell", "powershell.exe"}:
            return [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", cmd]
        if shell_name in {"cmd", "cmd.exe"}:
            return [shell, "/d", "/s", "/c", cmd]
        return [shell, "-lc", cmd]

    @staticmethod
    def _default_shell_path() -> str:
        if os.name == "nt":
            for candidate in ("pwsh.exe", "powershell.exe", "cmd.exe"):
                resolved = shutil.which(candidate)
                if resolved:
                    return resolved
            return "cmd.exe"
        for candidate in ("/bin/zsh", "/bin/bash", "/bin/sh"):
            if os.path.isfile(candidate):
                return candidate
        for candidate in ("zsh", "bash", "sh"):
            resolved = shutil.which(candidate)
            if resolved:
                return resolved
        return "/bin/sh"

    @staticmethod
    def _coerce_int(value: object, *, default: int, minimum: int) -> int:
        try:
            coerced = int(value) if value is not None else default
        except (TypeError, ValueError):
            coerced = default
        return max(minimum, coerced)

class ShellExecCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="exec",
        action_name="shell",
        description=SHELL_EXEC_DESCRIPTION,
        aliases=("shell_exec", "op_exec_run"),
        args_schema={
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": SHELL_EXEC_CMD_DESCRIPTION},
                "cwd": {"type": "string", "description": "Optional working directory."},
                "timeout_ms": {"type": "integer", "minimum": 1, "description": "Optional timeout in milliseconds."},
            },
            "required": ["cmd"],
        },
        result_schema={
            "type": "object",
            "properties": {
                "cmd": {"type": "string"},
                "cwd": {"type": "string"},
                "returncode": {"type": "integer"},
                "stdout": {"type": "string"},
                "stderr": {"type": "string"},
                "stdout_truncated": {"type": "boolean"},
                "stderr_truncated": {"type": "boolean"},
                "timeout_ms": {"type": "integer"},
            },
        },
    )
    def shell(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.runtime.execute_tool(type("ToolCall", (), {"name": "shell_exec", "args": dict(call.args)})())
        return IntrospectionResult(
            status=RuntimeStatus.OK if result.ok else RuntimeStatus.ERROR,
            text=result.text,
            structured=result.structured,
            llm_text=result.llm_text,
        )

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from pal.execution.contracts import CapabilityResult
from pal.shared import OPERATION_NAMESPACE, IntrospectionCall, IntrospectionResult, RuntimeStatus, capability_action
from pal.shared.result_rendering import render_head_tail_preview_for_llm


SHELL_EXEC_DESCRIPTION = (
    "Execute a shell command in the local runtime and return stdout, stderr, and exit status. "
    "Use this for tests, builds, scripts, process probes, package commands, and read-only git verification. "
    "When these capabilities are visible, use them before shell for their matching task: op_tree for structured directory listings; "
    "op_search for repository text search; op_file_read for reading text files; op_file_edit for precise in-place edits after reading; "
    "op_file_write for creating, overwriting, or appending UTF-8 text files; op_path_delete for deleting files or directories; "
    "op_file_state for checking read-before-edit cache state. "
    "Do not use shell commands such as cat/head/tail for file inspection, grep/rg for repository search, sed/awk for edits, "
    "tee/echo/printf redirection for writes, or rm/unlink/rmdir/git rm/find -delete for deletion when the matching capability is visible. "
    "Piping command output through head/tail to shorten stdout or stderr is fine. "
    "In minion workspaces, do not use shell for git add/commit or other checkpoint mutations; use the dedicated checkpoint commit capability instead."
)

SHELL_EXEC_CMD_DESCRIPTION = (
    "Shell command to execute. Use only for command execution, tests, builds, scripts, process probes, package commands, and read-only git verification. "
    "If visible, prefer op_tree for listings, op_search for text search, op_file_read for file reads, op_file_edit for edits, "
    "op_file_write for writes, op_path_delete for deletion, and op_file_state for file cache checks. "
    "Avoid cat/head/tail/grep/rg/sed/awk/tee/echo/printf redirection/rm/unlink/rmdir/git rm/find -delete for repo file operations when the matching capability is visible. "
    "In minion workspaces, do not run git add/commit/reset/checkout/clean/merge/rebase/push for checkpointing; use the dedicated checkpoint commit capability instead."
)

_SHELL_READ_COMMANDS = {"cat", "head", "tail"}
_SHELL_EDIT_COMMANDS = {"sed", "awk"}
_SHELL_WRITE_COMMANDS = {"tee"}
_SHELL_REDIRECT_WRITE_COMMANDS = {"cat", "echo", "printf"}
_SHELL_DELETE_COMMANDS = {"rm", "unlink", "rmdir"}


def _dedicated_tool_hint_for_shell_command(cmd: str) -> CapabilityResult | None:
    blocked = _blocked_shell_file_operation(cmd)
    if blocked is None:
        return None
    operation, command = blocked
    suggested = _suggested_tools_for_blocked_operation(operation)
    tool_text = ", ".join(suggested)
    text = (
        f"Use the dedicated Pal capability for this file operation instead of op_exec_shell. "
        f"Blocked shell command: {command}. Suggested capability: {tool_text}."
    )
    if operation == "read":
        text += " Use op_file_read with offset/limit for file inspection; head/tail remain okay for shortening command stdout or stderr."
    elif operation == "edit":
        text += " Use op_file_read first, then op_file_edit with old_string/new_string for precise edits."
    elif operation == "write":
        text += " Use op_file_write for create/overwrite/append instead of shell redirection."
    elif operation == "delete":
        text += " Use op_path_delete; set recursive=true only when deleting a directory."
    return CapabilityResult(
        status=RuntimeStatus.INVALID,
        text=text,
        structured={
            "reason": "dedicated_file_tool_required",
            "blocked_command": command,
            "operation": operation,
            "suggested_tools": suggested,
        },
        llm_text=text,
    )


def _blocked_shell_file_operation(cmd: str) -> tuple[str, str] | None:
    for segment in _shell_segments(cmd):
        tokens = _shell_tokens(segment)
        if not tokens:
            continue
        action = Path(tokens[0]).name
        if action in _SHELL_READ_COMMANDS and _read_command_targets_file(action, tokens):
            return "read", segment
        if action == "xargs" and any(Path(token).name in _SHELL_READ_COMMANDS for token in tokens[1:]):
            return "read", segment
        if action == "find":
            if _find_exec_read_command(tokens[1:]):
                return "read", segment
            if any(token == "-delete" for token in tokens[1:]):
                return "delete", segment
        if action in _SHELL_EDIT_COMMANDS and _edit_command_mutates_file(action, tokens):
            return "edit", segment
        if action in _SHELL_WRITE_COMMANDS:
            return "write", segment
        if action in _SHELL_REDIRECT_WRITE_COMMANDS and _segment_has_write_redirect(segment):
            return "write", segment
        if action in _SHELL_DELETE_COMMANDS:
            return "delete", segment
        if action == "git" and len(tokens) > 1 and tokens[1] == "rm":
            return "delete", segment
    return None


def _shell_segments(cmd: str) -> list[str]:
    return [segment.strip() for segment in re.split(r"\s*(?:&&|\|\||[|;])\s*", str(cmd or "")) if segment.strip()]


def _shell_tokens(segment: str) -> list[str]:
    try:
        return shlex.split(segment, posix=(os.name != "nt"))
    except ValueError:
        return []


def _read_command_targets_file(action: str, tokens: list[str]) -> bool:
    args = tokens[1:]
    if action == "cat":
        return _cat_args_target_file(args)
    if action in {"head", "tail"}:
        return _head_tail_args_target_file(args)
    return False


def _cat_args_target_file(args: list[str]) -> bool:
    for token in args:
        if token == "-":
            continue
        if token.startswith("-"):
            continue
        return True
    return False


def _head_tail_args_target_file(args: list[str]) -> bool:
    index = 0
    option_args = {"-n", "--lines", "-c", "--bytes"}
    while index < len(args):
        token = args[index]
        if token == "-":
            index += 1
            continue
        if token in option_args:
            index += 2
            continue
        if token.startswith(("--lines=", "--bytes=")):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return True
    return False


def _find_exec_read_command(args: list[str]) -> str:
    for index, token in enumerate(args):
        if token not in {"-exec", "-execdir"}:
            continue
        if index + 1 >= len(args):
            continue
        action = Path(args[index + 1]).name
        if action in _SHELL_READ_COMMANDS:
            return action
    return ""


def _edit_command_mutates_file(action: str, tokens: list[str]) -> bool:
    if action == "sed":
        return any(token == "-i" or token.startswith("-i") for token in tokens[1:])
    return _segment_has_write_redirect(" ".join(tokens))


def _segment_has_write_redirect(segment: str) -> bool:
    return bool(re.search(r">>|(?<![<])>(?![>&])", segment))


def _suggested_tools_for_blocked_operation(operation: str) -> list[str]:
    if operation == "read":
        return ["op_file_read"]
    if operation == "edit":
        return ["op_file_read", "op_file_edit"]
    if operation == "write":
        return ["op_file_write"]
    if operation == "delete":
        return ["op_path_delete"]
    return ["op_file_read", "op_file_edit", "op_file_write", "op_path_delete"]


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    preview, _preview_size = render_head_tail_preview_for_llm(text, max_chars=limit)
    return preview, True


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
    default_output_limit: int = 12_000
    shell_path: str = ""

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": SHELL_EXEC_CMD_DESCRIPTION},
                    "cwd": {"type": "string", "description": "Optional working directory."},
                    "timeout_ms": {"type": "integer", "minimum": 1, "description": "Optional timeout in milliseconds."},
                    "output_limit": {
                        "type": "integer",
                        "minimum": 256,
                        "description": "Maximum characters preserved for stdout and stderr.",
                    },
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
        dedicated_hint = _dedicated_tool_hint_for_shell_command(cmd)
        if dedicated_hint is not None:
            return dedicated_hint

        cwd_value = args.get("cwd")
        cwd = str(cwd_value).strip() if isinstance(cwd_value, str) and cwd_value.strip() else None
        timeout_ms = self._coerce_int(args.get("timeout_ms"), default=self.default_timeout_ms, minimum=1)
        output_limit = self._coerce_int(args.get("output_limit"), default=self.default_output_limit, minimum=256)

        completed = subprocess.run(
            self._build_shell_command(cmd),
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout_ms / 1000.0,
            check=False,
        )

        stdout, stdout_truncated = _truncate(completed.stdout or "", output_limit)
        stderr, stderr_truncated = _truncate(completed.stderr or "", output_limit)
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
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
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
        dedicated_hint = _dedicated_tool_hint_for_shell_command(cmd)
        if dedicated_hint is not None:
            return dedicated_hint
        cwd_value = args.get("cwd")
        cwd = str(cwd_value).strip() if isinstance(cwd_value, str) and cwd_value.strip() else None
        timeout_ms = self._coerce_int(args.get("timeout_ms"), default=self.default_timeout_ms, minimum=1)
        output_limit = self._coerce_int(args.get("output_limit"), default=self.default_output_limit, minimum=256)
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
        stdout, stdout_truncated = _truncate(stdout or "", output_limit)
        stderr, stderr_truncated = _truncate(stderr or "", output_limit)
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
                "stdout_truncated": stdout_truncated,
                "stderr_truncated": stderr_truncated,
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
                "output_limit": {
                    "type": "integer",
                    "minimum": 256,
                    "description": "Maximum characters preserved for stdout and stderr.",
                },
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

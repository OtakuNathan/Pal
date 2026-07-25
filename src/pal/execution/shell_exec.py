from __future__ import annotations

from pal.execution.generated_tool_models import (
    ExecutionShellExecShellExecCapabilityMixinShellInput,
    ExecutionShellExecShellExecCapabilityMixinShellOutput,
)

import asyncio
import contextlib
import os
import signal
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import ToolGuidance
from pal.execution.tool_semantics import DIRECT_CONTROL
from pal.shared import (
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
)


SHELL_EXEC_DESCRIPTION = "Run one shell command and return stdout, stderr, and exit status."

SHELL_EXEC_GUIDANCE = ToolGuidance(
    purpose=SHELL_EXEC_DESCRIPTION,
    use_when=(
        "Use for tests, builds, scripts, package commands, process probes, and bounded directory listings. "
        "Run long-lived tests and builds directly so their complete stdout and stderr remain available."
    ),
    do_not_use_when=(
        "Do not use for Pal runtime, module, capability, or Minion state when a Pal tool is available. "
        "When the matching dedicated capability is visible, use search for repository text search, read_file for "
        "file reads, edit_file for edits, write_file for writes, delete_path for deletion, and git for Git operations. "
        "Do not pipe long-running tests or builds through head, tail, or grep merely to shorten their result; result "
        "budgeting handles large output, while such pipelines hide the command that is stalled."
    ),
    failure_next_steps=(
        "Inspect stdout, stderr, and exit status, correct the command or environment, and retry only when the "
        "operation is safe to repeat. Follow any returned recovery affordance."
    ),
)

SHELL_EXEC_CMD_DESCRIPTION = (
    "Shell command to execute as one string. "
    "Use bounded `tree -a -L 3 --filelimit 200 --noreport` listings (or `find -maxdepth 3 -print | head -n 500` when tree is unavailable). "
    "Pipelines and shell operators are accepted; use them only when they are part of the command being executed."
)

DEFAULT_SHELL_TIMEOUT_MS = 120_000
MAX_SHELL_TIMEOUT_MS = 600_000
SHELL_TERMINATION_GRACE_SECONDS = 1.0
SHELL_OUTPUT_ROOT = Path("/tmp/pal") if os.name != "nt" else Path(tempfile.gettempdir()) / "pal"


@dataclass(frozen=True)
class _ShellExecution:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    termination_signal: str = ""
    descendants_terminated: bool = False


@dataclass(eq=False)
class _ShellProcessSupervisor:
    argv: list[str]
    cwd: str | None
    timeout_ms: int
    output_root: Path
    termination_grace_seconds: float = SHELL_TERMINATION_GRACE_SECONDS
    _proc: subprocess.Popen[bytes] | None = field(default=None, init=False, repr=False)
    _cancel_requested: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _state_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _termination_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _termination_signal: str = field(default="", init=False, repr=False)
    _descendants_terminated: bool = field(default=False, init=False, repr=False)

    def run(self) -> _ShellExecution:
        self.output_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="shell-", dir=self.output_root) as output_dir:
            stdout_path = Path(output_dir) / "stdout"
            stderr_path = Path(output_dir) / "stderr"
            timed_out = False
            with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
                if self._cancel_requested.is_set():
                    return _ShellExecution(returncode=None, stdout="", stderr="", cancelled=True)
                proc = subprocess.Popen(
                    self.argv,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    cwd=self.cwd,
                    start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                )
                with self._state_lock:
                    self._proc = proc
                if self._cancel_requested.is_set():
                    self._terminate_current_process()
                try:
                    proc.wait(timeout=self.timeout_ms / 1000.0)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self._terminate_current_process()
                else:
                    if self._cancel_requested.is_set():
                        # Synchronize with an interrupt-side terminator before
                        # snapshotting termination metadata below.
                        self._terminate_current_process(proc)
                    else:
                        self._cleanup_normal_exit_descendants(proc)
                finally:
                    with contextlib.suppress(Exception):
                        proc.wait(timeout=self.termination_grace_seconds + 1.0)
                    stdout_file.flush()
                    stderr_file.flush()
                    with self._state_lock:
                        self._proc = None
            stdout = stdout_path.read_bytes().decode("utf-8", errors="replace")
            stderr = stderr_path.read_bytes().decode("utf-8", errors="replace")
            return _ShellExecution(
                returncode=proc.returncode,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
                cancelled=self._cancel_requested.is_set() and not timed_out,
                termination_signal=self._termination_signal,
                descendants_terminated=self._descendants_terminated,
            )

    async def cancel(self) -> None:
        await asyncio.to_thread(self.terminate)

    def terminate(self) -> None:
        self._cancel_requested.set()
        self._terminate_current_process()

    def _cleanup_normal_exit_descendants(self, proc: subprocess.Popen[bytes]) -> None:
        if os.name == "nt":
            return
        if not _posix_process_group_exists(proc.pid):
            return
        self._descendants_terminated = True
        self._terminate_current_process(proc)

    def _terminate_current_process(self, expected: subprocess.Popen[bytes] | None = None) -> None:
        with self._termination_lock:
            with self._state_lock:
                proc = self._proc
            if proc is None or (expected is not None and proc is not expected):
                return
            if os.name == "nt":
                termination_signal = _terminate_windows_process_tree(proc)
            else:
                termination_signal = _terminate_posix_process_group(
                    proc,
                    grace_seconds=self.termination_grace_seconds,
                )
            if termination_signal:
                self._termination_signal = termination_signal
                # The termination target is the whole POSIX process group or
                # Windows process tree, not only the outer shell process.
                self._descendants_terminated = True


def _terminate_posix_process_group(
    proc: subprocess.Popen[bytes],
    *,
    grace_seconds: float,
) -> str:
    process_group = proc.pid
    if not _posix_process_group_exists(process_group):
        with contextlib.suppress(Exception):
            proc.wait(timeout=0)
        return ""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGTERM)
    if _wait_for_posix_process_group_exit(process_group, timeout=grace_seconds):
        with contextlib.suppress(Exception):
            proc.wait(timeout=0)
        return "SIGTERM"
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGKILL)
    _wait_for_posix_process_group_exit(process_group, timeout=grace_seconds)
    with contextlib.suppress(Exception):
        proc.wait(timeout=grace_seconds)
    return "SIGKILL"


def _posix_process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_posix_process_group_exit(process_group: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while _posix_process_group_exists(process_group):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)
    return True


def _terminate_windows_process_tree(proc: subprocess.Popen[bytes]) -> str:
    if proc.poll() is not None:
        return ""
    completed = subprocess.run(
        ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    with contextlib.suppress(Exception):
        proc.wait(timeout=1)
    return "taskkill" if completed.returncode == 0 else ""


@dataclass
class ShellExecTool:
    default_timeout_ms: int = DEFAULT_SHELL_TIMEOUT_MS
    max_timeout_ms: int = MAX_SHELL_TIMEOUT_MS
    shell_path: str = ""
    output_root: Path = SHELL_OUTPUT_ROOT

    def __post_init__(self) -> None:
        if not self.shell_path:
            self.shell_path = self._default_shell_path()

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        prepared = self._prepare(args)
        if isinstance(prepared, CapabilityResult):
            return prepared
        cmd, cwd, timeout_ms, supervisor = prepared
        try:
            execution = supervisor.run()
        except OSError as exc:
            return self._spawn_failure(cmd, cwd, timeout_ms, exc)
        return self._execution_result(cmd, cwd, timeout_ms, execution)

    async def ainvoke(self, args: dict[str, object], **kwargs: object) -> CapabilityResult:
        runtime = kwargs.get("runtime")
        turn_id = str(kwargs.get("turn_id") or "").strip() or None
        prepared = self._prepare(args)
        if isinstance(prepared, CapabilityResult):
            return prepared
        cmd, cwd, timeout_ms, supervisor = prepared
        register = getattr(runtime, "register_interrupt_handle", None)
        if callable(register):
            register(turn_id, supervisor)
        try:
            execution = await asyncio.to_thread(supervisor.run)
        except asyncio.CancelledError:
            await supervisor.cancel()
            raise
        except OSError as exc:
            return self._spawn_failure(cmd, cwd, timeout_ms, exc)
        finally:
            release = getattr(runtime, "release_interrupt_handle", None)
            if callable(release):
                release(turn_id, supervisor)
        return self._execution_result(cmd, cwd, timeout_ms, execution)

    def _prepare(
        self,
        args: dict[str, object],
    ) -> tuple[str, str | None, int, _ShellProcessSupervisor] | CapabilityResult:
        cmd = str(args.get("cmd") or "").strip()
        if not cmd:
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text="cmd is required",
                structured={"error_code": "missing_cmd"},
                llm_text="cmd is required",
            )
        cwd_value = args.get("cwd")
        cwd = str(cwd_value).strip() if isinstance(cwd_value, str) and cwd_value.strip() else None
        timeout_ms = self._coerce_int(
            args.get("timeout_ms"),
            default=self.default_timeout_ms,
            minimum=1,
            maximum=self.max_timeout_ms,
        )
        supervisor = _ShellProcessSupervisor(
            argv=self._build_shell_command(cmd),
            cwd=cwd,
            timeout_ms=timeout_ms,
            output_root=self.output_root,
        )
        return cmd, cwd, timeout_ms, supervisor

    @staticmethod
    def _execution_result(
        cmd: str,
        cwd: str | None,
        timeout_ms: int,
        execution: _ShellExecution,
    ) -> CapabilityResult:
        if execution.timed_out:
            display_text = f"command timed out after {timeout_ms} ms"
            error_code = "command_timed_out"
        elif execution.cancelled:
            display_text = "command was cancelled"
            error_code = "command_cancelled"
        else:
            ok = execution.returncode == 0
            display_text = (execution.stdout if ok else execution.stderr or execution.stdout).strip()
            if not display_text:
                display_text = f"command exited with code {execution.returncode}"
            error_code = "" if ok else "command_failed"
        output_text = _render_shell_output(display_text, execution.stdout, execution.stderr)
        structured = {
            "cmd": cmd,
            "cwd": cwd or str(Path.cwd()),
            "display_text": display_text,
            "returncode": execution.returncode,
            "stdout": execution.stdout,
            "stderr": execution.stderr,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "timeout_ms": timeout_ms,
            "timed_out": execution.timed_out,
            "cancelled": execution.cancelled,
            "termination_signal": execution.termination_signal,
            "descendants_terminated": execution.descendants_terminated,
        }
        if error_code:
            structured["error_code"] = error_code
        return CapabilityResult(
            status=RuntimeStatus.OK if not error_code else RuntimeStatus.ERROR,
            text=output_text,
            structured=structured,
            llm_text=output_text,
        )

    @staticmethod
    def _spawn_failure(
        cmd: str,
        cwd: str | None,
        timeout_ms: int,
        exc: OSError,
    ) -> CapabilityResult:
        display_text = f"could not start shell command: {type(exc).__name__}: {exc}"
        return CapabilityResult(
            status=RuntimeStatus.ERROR,
            text=display_text,
            llm_text=display_text,
            structured={
                "error_code": "shell_spawn_failed",
                "cmd": cmd,
                "cwd": cwd or str(Path.cwd()),
                "display_text": display_text,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "stdout_truncated": False,
                "stderr_truncated": False,
                "timeout_ms": timeout_ms,
                "timed_out": False,
                "cancelled": False,
                "termination_signal": "",
                "descendants_terminated": False,
            },
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
    def _coerce_int(
        value: object,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            coerced = int(value) if value is not None else default
        except (TypeError, ValueError):
            coerced = default
        return min(maximum, max(minimum, coerced))


def _render_shell_output(display_text: str, stdout: str, stderr: str) -> str:
    sections = [display_text]
    stripped_stdout = stdout.strip()
    stripped_stderr = stderr.strip()
    if stripped_stdout and stripped_stdout != display_text:
        sections.extend(("stdout:", stripped_stdout))
    if stripped_stderr and stripped_stderr != display_text:
        sections.extend(("stderr:", stripped_stderr))
    return "\n".join(sections)


class ShellExecCapabilityMixin:
    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="module",
        family="exec",
        action_name="shell",
        description=SHELL_EXEC_DESCRIPTION,
        aliases=("run_shell",),
        InputModel=ExecutionShellExecShellExecCapabilityMixinShellInput,
        OutputModel=ExecutionShellExecShellExecCapabilityMixinShellOutput,
        guidance=SHELL_EXEC_GUIDANCE,
        execution=DIRECT_CONTROL,
        async_handler_name="shell_async",
    )
    def shell(self, call: IntrospectionCall) -> IntrospectionResult:
        return ShellExecTool().invoke(dict(call.args))

    async def shell_async(self, call: IntrospectionCall) -> IntrospectionResult:
        return await ShellExecTool().ainvoke(
            dict(call.args),
            runtime=call.meta.get("execution_runtime"),
            turn_id=call.meta.get("turn_id"),
        )

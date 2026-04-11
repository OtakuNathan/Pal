from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pal.execution.contracts import CapabilityResult
from pal.shared import OPERATION_NAMESPACE, IntrospectionCall, IntrospectionResult, RuntimeStatus, capability_action


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


@dataclass
class ShellExecTool:
    name: str = "shell.exec"
    display_name: str = "Shell Exec"
    family: str = "system"
    description: str = "Execute a shell command in the local runtime and return stdout, stderr, and exit status."
    tags: tuple[str, ...] = ("shell", "system", "exec")
    keywords: tuple[str, ...] = ("command", "terminal", "bash", "zsh", "run")
    args_schema: dict[str, object] = None  # type: ignore[assignment]   
    result_schema: dict[str, object] = None  # type: ignore[assignment]
    default_timeout_ms: int = 30_000
    default_output_limit: int = 12_000
    shell_path: str = "/bin/zsh" if os.path.isfile("/bin/zsh") else "/bin/bash"

    def __post_init__(self) -> None:
        if self.args_schema is None:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "Shell command to execute."},
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
        output_limit = self._coerce_int(args.get("output_limit"), default=self.default_output_limit, minimum=256)

        completed = subprocess.run(
            [self.shell_path, "-lc", cmd],
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
        action_name="run",
        description="Execute a shell command in the local runtime.",
        aliases=("shell.exec",),
        args_schema={
            "type": "object",
            "properties": {
                "cmd": {"type": "string", "description": "Shell command to execute."},
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
        metadata={"llm_exposed": True},
    )
    def run(self, call: IntrospectionCall) -> IntrospectionResult:
        result = self.runtime.execute_tool(type("ToolCall", (), {"name": "shell.exec", "args": dict(call.args)})())
        return IntrospectionResult(
            status=RuntimeStatus.OK if result.ok else RuntimeStatus.ERROR,
            text=result.text,
            structured=result.structured,
            llm_text=result.llm_text,
        )

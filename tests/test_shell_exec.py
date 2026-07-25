from __future__ import annotations

import asyncio
import os
import shlex
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pal.core import PalCore
from pal.execution import register_with_core as register_execution_with_core
from pal.execution.shell_exec import (
    MAX_SHELL_TIMEOUT_MS,
    SHELL_OUTPUT_ROOT,
    ShellExecTool,
)
from pal.llm import CanonicalToolCall


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_file(path: Path, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"timed out waiting for {path}")
        time.sleep(0.01)


def _wait_for_process_exit(pid: int, *, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while _process_exists(pid):
        if time.monotonic() >= deadline:
            raise AssertionError(f"process {pid} is still alive")
        time.sleep(0.02)


class ShellExecToolTests(unittest.TestCase):
    def test_default_output_root_and_timeout_bound_match_shell_contract(self) -> None:
        self.assertEqual(SHELL_OUTPUT_ROOT, Path("/tmp/pal"))
        self.assertEqual(MAX_SHELL_TIMEOUT_MS, 600_000)

    def test_timeout_kills_pipeline_process_group_and_preserves_partial_output(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX process-group behavior")
        with tempfile.TemporaryDirectory(prefix="pal_shell_test_") as tmp:
            root = Path(tmp)
            pid_path = root / "child.pid"
            output_root = root / "output"
            source = (
                "import os, pathlib, signal, sys, time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()));"
                "print('partial-stdout', flush=True);"
                "print('partial-stderr', file=sys.stderr, flush=True);"
                "time.sleep(60)"
            )
            result = ShellExecTool(output_root=output_root).invoke(
                {
                    "cmd": f"{_python_command(source)} | cat",
                    "timeout_ms": 500,
                }
            )

            _wait_for_file(pid_path)
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            _wait_for_process_exit(child_pid)
            self.assertEqual(result.status, "error")
            self.assertEqual(result.structured["error_code"], "command_timed_out")
            self.assertTrue(result.structured["timed_out"])
            self.assertEqual(result.structured["termination_signal"], "SIGKILL")
            self.assertTrue(result.structured["descendants_terminated"])
            self.assertIn("partial-stdout", result.structured["stdout"])
            self.assertIn("partial-stderr", result.structured["stderr"])
            self.assertIn("partial-stdout", result.llm_text)
            self.assertIn("partial-stderr", result.llm_text)
            self.assertEqual(list(output_root.glob("shell-*")), [])

    def test_stdin_is_non_interactive(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_shell_test_") as tmp:
            result = ShellExecTool(output_root=Path(tmp) / "output").invoke(
                {
                    "cmd": _python_command("input()"),
                    "timeout_ms": 2_000,
                }
            )

            self.assertEqual(result.status, "error")
            self.assertFalse(result.structured["timed_out"])
            self.assertIn("EOFError", result.structured["stderr"])

    def test_shell_exit_does_not_wait_for_or_leak_background_descendants(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX process-group behavior")
        with tempfile.TemporaryDirectory(prefix="pal_shell_test_") as tmp:
            root = Path(tmp)
            pid_path = root / "background.pid"
            output_root = root / "output"
            source = "import time; time.sleep(60)"

            started = time.monotonic()
            result = ShellExecTool(output_root=output_root).invoke(
                {
                    "cmd": (
                        f"{_python_command(source)} & "
                        f"child_pid=$!; printf '%s' \"$child_pid\" > {shlex.quote(str(pid_path))}"
                    ),
                    "timeout_ms": 5_000,
                }
            )
            elapsed = time.monotonic() - started

            _wait_for_file(pid_path)
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            _wait_for_process_exit(child_pid)
            self.assertEqual(result.status, "ok")
            self.assertLess(elapsed, 4)
            self.assertTrue(result.structured["descendants_terminated"])
            self.assertIn(result.structured["termination_signal"], {"SIGTERM", "SIGKILL"})
            self.assertEqual(list(output_root.glob("shell-*")), [])

    def test_timeout_is_capped_even_for_direct_tool_use(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_shell_test_") as tmp:
            tool = ShellExecTool(output_root=Path(tmp) / "output")
            result = tool.invoke({"cmd": "true", "timeout_ms": MAX_SHELL_TIMEOUT_MS + 1})

            self.assertEqual(result.status, "ok")
            self.assertEqual(result.structured["timeout_ms"], MAX_SHELL_TIMEOUT_MS)


class ShellExecAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_compiled_capability_uses_async_handler(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        record = core.context.execution_runtime.registry_generation.direct_aliases["run_shell"]

        self.assertIsNotNone(record.binding.async_callable)
        result = await core.context.execution_runtime.execute_tool_async(
            CanonicalToolCall(name="run_shell", args={"cmd": "printf async-ok"}),
            turn_id="shell-turn",
        )

        self.assertTrue(result.ok, result.text)
        self.assertEqual(result.structured["stdout"], "async-ok")

    async def test_interrupt_kills_registered_process_group(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX process-group behavior")
        with tempfile.TemporaryDirectory(prefix="pal_shell_test_") as tmp:
            root = Path(tmp)
            pid_path = root / "child.pid"
            core = PalCore()
            register_execution_with_core(core.context)
            core.publish_module_capabilities("execution")
            source = (
                "import os, pathlib, signal, time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()));"
                "time.sleep(60)"
            )
            task = asyncio.create_task(
                core.context.execution_runtime.execute_tool_async(
                    CanonicalToolCall(
                        name="run_shell",
                        args={"cmd": _python_command(source), "timeout_ms": 10_000},
                    ),
                    turn_id="interrupt-shell-turn",
                )
            )
            await asyncio.to_thread(_wait_for_file, pid_path)
            child_pid = int(pid_path.read_text(encoding="utf-8"))

            await core.context.execution_runtime.interrupt_turn("interrupt-shell-turn")
            result = await asyncio.wait_for(task, timeout=4)

            await asyncio.to_thread(_wait_for_process_exit, child_pid)
            self.assertFalse(result.ok)
            self.assertEqual(result.structured["error_code"], "command_cancelled")
            details = result.structured["details"]
            self.assertTrue(details["cancelled"])
            self.assertTrue(details["descendants_terminated"])
            self.assertIn(
                details["termination_signal"],
                {signal.Signals.SIGTERM.name, signal.Signals.SIGKILL.name},
            )

    async def test_runtime_shutdown_kills_registered_process_group(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX process-group behavior")
        with tempfile.TemporaryDirectory(prefix="pal_shell_test_") as tmp:
            root = Path(tmp)
            pid_path = root / "child.pid"
            core = PalCore()
            register_execution_with_core(core.context)
            core.publish_module_capabilities("execution")
            source = (
                "import os, pathlib, signal, time;"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(os.getpid()));"
                "time.sleep(60)"
            )
            task = asyncio.create_task(
                core.context.execution_runtime.execute_tool_async(
                    CanonicalToolCall(
                        name="run_shell",
                        args={"cmd": _python_command(source), "timeout_ms": 10_000},
                    ),
                    turn_id="shutdown-shell-turn",
                )
            )
            await asyncio.to_thread(_wait_for_file, pid_path)
            child_pid = int(pid_path.read_text(encoding="utf-8"))

            await asyncio.to_thread(core.context.execution_runtime.shutdown)
            result = await asyncio.wait_for(task, timeout=4)

            await asyncio.to_thread(_wait_for_process_exit, child_pid)
            self.assertFalse(result.ok)
            self.assertEqual(result.structured["error_code"], "command_cancelled")


if __name__ == "__main__":
    unittest.main()

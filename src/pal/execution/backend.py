"""Select the same execution backend for resident and role runtimes."""
import os

from .runtime import ExecutionRuntime


def build_execution_runtime(**kwargs):
    backend = os.environ.get("PAL_SHELL_BACKEND", "python")
    if backend == "python":
        return ExecutionRuntime(**kwargs)
    if backend == "native":
        from .native_shell.runtime import NativeExecutionRuntime
        return NativeExecutionRuntime(**kwargs)
    raise ValueError("PAL_SHELL_BACKEND must be python or native")

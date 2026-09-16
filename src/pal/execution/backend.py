"""Build the default execution implementation; plugins own optional replacements."""
from .runtime import ExecutionRuntime


def build_execution_runtime(**kwargs):
    return ExecutionRuntime(**kwargs)

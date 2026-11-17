"""Explicit in-memory composition for runtime tests."""
from pal.core.main_context import MainContext
from pal.core.runtime import PalCore
from pal.execution.backend import build_execution_runtime


def build_context(**options):
    if "execution_runtime" not in options:
        options["execution_runtime"] = build_execution_runtime()
    return MainContext(**options)


def build_core(**options):
    if "context" not in options:
        options["context"] = build_context()
    core = PalCore(**options)
    from pal.control.runtime_contracts import CORE_CONTROL
    core.context.port_registry.register(CORE_CONTROL, core.control_runtime)
    return core


def mount_l3_provider(context, provider):
    from pal.memory.service import MemoryService
    from pal.memory.capabilities import register_with_core as register_memory
    from pal.plugins.l3.capabilities import register_with_core
    if context.port_registry.get("memory:memory") is None:
        register_memory(context, MemoryService())
    return register_with_core(context, provider)

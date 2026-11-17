"""Explicit composition of module-owned control declarations for parser tests."""
from pal.control.service import ControlPlane
from pal.core import commands as core_commands
from pal.control import commands as control_commands
from pal.llm import commands as llm_commands
from pal.memory import commands as memory_commands


def build_control_plane() -> ControlPlane:
    plane = ControlPlane()
    for owner, commands in (("core", core_commands), ("control", control_commands), ("llm", llm_commands)):
        plane.install_commands(owner, commands.command_specs(), interactions=commands.interaction_specs())
    plane.install_commands("memory", memory_commands.command_specs())
    return plane


def replace_llm(core, runtime):
    from pal.llm.capabilities import register_with_core
    previous = core.context.module_registry.get("llm")
    if previous is not None:
        core.withdraw_module_capabilities("llm")
        core.context.unregister_module(previous)
    return register_with_core(core.context, runtime)

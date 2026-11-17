"""Plugin-owned activation boundary for prepared package file replacements."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Any

from pal.packages.process import PackageError, check_cancelled
from pal.shared import RuntimeStatus

if TYPE_CHECKING:
    from pal.plugins.host import PluginHost


@dataclass(frozen=True)
class PluginReplacementState:
    existed: bool
    attached: bool
    enabled: bool


class PluginPackageLifecycle:
    def __init__(self, host: PluginHost) -> None:
        self.host = host

    def is_builtin(self, name: str) -> bool:
        return name in self.host.first_party_records

    def removal_checkpoint(self) -> Callable[[], bool]:
        generations = tuple((generation, generation.cleanup_started) for generation in self.host.generations.values())
        return lambda: any(not started and generation.cleanup_started for generation, started in generations)

    def detach_for_removal(self, name: str) -> dict[str, Any] | None:
        if self.host._record(name) is None and name not in self.host.generations and name not in self.host._pending_rollbacks:
            return None
        result = self.host.detach(name)
        if result["status"] == RuntimeStatus.OK and name in self.host.generations:
            return {**result, "status": RuntimeStatus.ERROR, "cleanup_pending": True}
        return result

    def forget_uninstalled(self, name: str) -> None:
        self.host.forget_uninstalled(name)

    def gate(self):
        return self.host.context.execution_runtime.lifecycle_gate.write()

    @contextmanager
    def switch(self, name: str):
        check_cancelled()
        captured = self.host.withdraw_tool_entries(name)
        entered = False
        try:
            with self.gate():
                entered = True
                try:
                    yield
                finally:
                    if captured:
                        self.host.restore_tool_entries(captured)
        finally:
            if not entered and captured:
                self.host.restore_tool_entries(captured)

    def begin_replacement(self, name: str) -> PluginReplacementState:
        record = self.host._record(name)
        state = PluginReplacementState(existed=record is not None,
            attached=bool(record and record.attached), enabled=bool(record and record.enabled))
        owned = name in self.host.generations or name in self.host._pending_rollbacks
        if (state.attached or owned) and self.host.detach(name)["status"] != RuntimeStatus.OK:
            raise PackageError("Previous plugin could not be detached; installation was not switched")
        return state

    def finish_replacement(self, name: str, state: PluginReplacementState) -> str:
        scan = self.host.rescan()
        errors = [error for error in scan.get("scan_errors", [])
                  if f"/{name}/plugin.toml" in error or f"/{name}/provider.toml" in error]
        if errors:
            raise PackageError(f"Plugin discovery failed: {errors}")
        record = self.host._record(name)
        if record is None:
            raise PackageError("Installed plugin was not discovered")
        should_attach = state.attached if state.existed else record.enabled
        if should_attach and self.host.attach(name)["status"] != RuntimeStatus.OK:
            raise PackageError(f"Plugin attach failed: {self.host._record(name).last_error}")
        return "attached" if should_attach else "installed_inactive"

    def restore_replacement(self, name: str, state: PluginReplacementState) -> None:
        if state.existed:
            self.finish_replacement(name, state)
        else:
            self.host.rescan()

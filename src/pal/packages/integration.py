"""Package preparation delegates runtime activation to its existing owner."""
from __future__ import annotations

from typing import TYPE_CHECKING

from pal.packages.process import PackageError

if TYPE_CHECKING:
    from pal.plugins.host import PluginHost
    from pal.plugins.package_lifecycle import PluginReplacementState


class RuntimeActivation:
    def __init__(self, host: PluginHost):
        self.owner = host.package_lifecycle

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in {"plugin", "provider"}:
            raise PackageError("Only plugin and channel-provider packages have runtime activation")

    def gate(self):
        return self.owner.gate()

    def switch_gate(self, kind: str, name: str):
        self._validate_kind(kind)
        return self.owner.switch(name)

    def before(self, kind: str, name: str) -> PluginReplacementState:
        self._validate_kind(kind)
        return self.owner.begin_replacement(name)

    def after(self, kind: str, name: str, state: PluginReplacementState) -> str:
        self._validate_kind(kind)
        return self.owner.finish_replacement(name, state)

    def restore(self, kind: str, name: str, state: PluginReplacementState) -> None:
        self._validate_kind(kind)
        self.owner.restore_replacement(name, state)

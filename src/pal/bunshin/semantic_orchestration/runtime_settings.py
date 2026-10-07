"""Mutable policy shared by future attempts; each attempt snapshots it into its pack."""
from dataclasses import dataclass


@dataclass
class RoleRuntimeSettings:
    _prompt_logging: bool = False

    @property
    def prompt_logging(self) -> bool:
        return self._prompt_logging

    def set_prompt_logging(self, enabled: bool) -> None:
        self._prompt_logging = bool(enabled)

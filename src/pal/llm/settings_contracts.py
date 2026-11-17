"""The LLM module's persisted runtime settings port."""
from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol

from pal.shared.ports import PortKey


class RuntimeSettingsPort(Protocol):
    def get(self, setting_key: str, /) -> str | None: ...
    def set(self, setting_key: str, setting_value: str, /) -> Any: ...
    def selection_transaction(self) -> AbstractContextManager[Any]: ...


LLM_SETTINGS = PortKey[RuntimeSettingsPort]("llm:settings", RuntimeSettingsPort)

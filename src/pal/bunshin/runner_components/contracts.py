"""Contracts used at a Bunshin role's execution and reporting boundaries."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from pal.shared import ToolExecutionResult
from pal.shared.tool_protocol import ToolCallIR

CancelCheck = Callable[[], Awaitable[None]]


class ToolExecutionPort(Protocol):
    async def execute_tool_async(
        self, call: ToolCallIR, *, allow_tools: bool = True,
        budget: Any = None, turn_id: str | None = None,
    ) -> ToolExecutionResult: ...


class RoleExecutionSessions(Protocol):
    @property
    def has_work(self) -> bool: ...

    @property
    def resources_live(self) -> bool: ...

    def handles_capability(self, name: str) -> bool: ...

    def execution_delegate(
        self, runtime: ToolExecutionPort, check_cancel: CancelCheck,
    ) -> ToolExecutionPort: ...

    async def wait_after_response(self, check_cancel: CancelCheck) -> str: ...

    def retry_note(self) -> str: ...

from __future__ import annotations
from typing import Any, Protocol
from pal.shared.ports import PortKey


class BackgroundWorkflowPort(Protocol):
    async def start_scheduled_run(self, request: dict[str, Any]) -> dict[str, Any]: ...
    async def scheduled_run_status(self, workflow_id: str) -> dict[str, Any]: ...


BACKGROUND_WORKFLOWS = PortKey[BackgroundWorkflowPort]("bunshin:background_workflows", BackgroundWorkflowPort)

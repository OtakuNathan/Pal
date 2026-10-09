from __future__ import annotations

from typing import Any, Protocol
from pal.mcp.model import McpPromptSpec, McpToolSpec


class McpConnector(Protocol):
    server_info: dict[str, Any]
    server_capabilities: dict[str, Any]

    def stderr_tail(self, limit: int = 20) -> tuple[str, ...]: ...

    def diagnostics(self) -> dict[str, Any]: ...

    async def initialize(self) -> None:
        ...

    async def list_tools_all(self) -> tuple[McpToolSpec, ...]:
        ...

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def list_prompts_all(self) -> tuple[McpPromptSpec, ...]:
        ...

    async def get_prompt(self, prompt_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        ...

    async def close(self) -> None:
        ...


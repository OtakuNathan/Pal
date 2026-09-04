from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pal.web_fetch.browser_service import BrowserServiceManager


@dataclass
class WebFetchService:
    """Plugin-owned facade over the authenticated browser sidecar."""

    browser_manager: BrowserServiceManager

    def execute(
        self,
        *,
        session_key: str,
        action: str,
        args: dict[str, Any] | None = None,
        persistent: bool = True,
        timeout_ms: int = 15000,
    ) -> dict[str, Any]:
        return self.browser_manager.execute(
            session_key=session_key,
            action=action,
            args=dict(args or {}),
            persistent=persistent,
            timeout_ms=timeout_ms,
        )

    def health(self) -> dict[str, Any]:
        return self.browser_manager.health()

    async def shutdown_async(self) -> None:
        await self.browser_manager.shutdown_async()

    def shutdown_sync(self) -> None:
        self.browser_manager.stop_sync()

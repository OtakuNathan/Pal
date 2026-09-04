from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pal.web_fetch import BrowserServiceManager, WebFetchService, register_with_core as register_web_fetch_with_core


@dataclass
class WebFetchBuiltinBundle:
    runtime_root: Path
    plugin_id: str = "web_fetch"
    version: str = "0.2.0"

    def start(self, scope):
        service = WebFetchService(
            browser_manager=BrowserServiceManager(runtime_root=self.runtime_root),
        )
        return register_web_fetch_with_core(scope.context, service)


def build_plugin(*, runtime_root: Path) -> WebFetchBuiltinBundle:
    return WebFetchBuiltinBundle(runtime_root=runtime_root)

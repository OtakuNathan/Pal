from __future__ import annotations

from dataclasses import dataclass

from pal.llm.repository import RuntimeSettingRepository
from pal.web_search import WebSearchProviderRepository, WebSearchService, register_with_core as register_web_search_with_core


@dataclass
class WebSearchBuiltinBundle:
    plugin_id: str = "web_search"
    version: str = "0.1.0"

    def start(self, scope):
        service = WebSearchService(
            repository=WebSearchProviderRepository(),
            settings_repository=RuntimeSettingRepository(),
        )
        return register_web_search_with_core(scope.context, service)


def build_plugin() -> WebSearchBuiltinBundle:
    return WebSearchBuiltinBundle()

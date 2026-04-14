from __future__ import annotations

from dataclasses import dataclass

from pal.llm.repository import RuntimeSettingRepository
from pal.web_search import WebSearchProviderRepository, WebSearchService, register_with_core as register_web_search_with_core


@dataclass
class WebSearchBuiltinBundle:
    plugin_id: str = "web_search"
    version: str = "0.1.0"

    def register_with_core(self, context):
        service = WebSearchService(
            repository=WebSearchProviderRepository(),
            settings_repository=RuntimeSettingRepository(),
        )
        return register_web_search_with_core(context, service)


def build_plugin() -> WebSearchBuiltinBundle:
    return WebSearchBuiltinBundle()

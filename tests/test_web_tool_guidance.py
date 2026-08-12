from __future__ import annotations

import unittest
from types import SimpleNamespace

from pal.shared import IntrospectionCall, RuntimeStatus
from pal.web_fetch.capabilities import WebFetchIntrospectionProvider
from pal.web_search.capabilities import WebSearchIntrospectionProvider


class _DisabledProviderService:
    def __init__(self) -> None:
        self.selected = False

    @staticmethod
    def get_provider(name: str):
        return SimpleNamespace(provider_id=name, enabled=False)

    def set_active_provider(self, _name: str):
        self.selected = True
        raise AssertionError("a disabled provider must not become configured active")


class WebToolGuidanceTests(unittest.TestCase):
    def test_search_rejects_disabled_provider_before_selection(self) -> None:
        service = _DisabledProviderService()
        provider = WebSearchIntrospectionProvider(service=service)  # type: ignore[arg-type]

        result = provider.set_active_provider(
            IntrospectionCall(name="web_search_set_active_provider", args={"name": "disabled"})
        )

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["reason"], "provider_disabled")
        self.assertFalse(service.selected)

    def test_fetch_rejects_disabled_provider_before_selection(self) -> None:
        service = _DisabledProviderService()
        provider = WebFetchIntrospectionProvider(service=service)  # type: ignore[arg-type]

        result = provider.set_active_provider(
            IntrospectionCall(name="web_fetch_set_active_provider", args={"name": "disabled"})
        )

        self.assertEqual(result.status, RuntimeStatus.INVALID)
        self.assertEqual(result.structured["reason"], "provider_disabled")
        self.assertFalse(service.selected)


if __name__ == "__main__":
    unittest.main()

from pal.web_fetch.browser_service import BrowserServiceManager, plain_http_fetch, playwright_python_available, run_browser_service_cli
from pal.web_fetch.contracts import (
    ACTIVE_WEB_FETCH_PROVIDER_SETTING_KEY,
    DEFAULT_WEB_FETCH_USER_AGENT,
    WebFetchDocument,
    WebFetchLink,
    WebFetchRequest,
    WebFetchResult,
    WebLayoutInspectionRequest,
    WebLayoutInspectionResult,
    WebScreenshotRequest,
    WebScreenshotResult,
)
from pal.web_fetch.capabilities import WebFetchIntrospectionProvider, WebFetchModuleSnapshot, inspect_web_fetch, register_with_core
from pal.web_fetch.models import WebFetchProviderModel
from pal.web_fetch.repository import WebFetchProviderRepository
from pal.web_fetch.service import WebFetchService
from pal.web_fetch.tools import WebScreenshotTool

__all__ = [
    "ACTIVE_WEB_FETCH_PROVIDER_SETTING_KEY",
    "BrowserServiceManager",
    "DEFAULT_WEB_FETCH_USER_AGENT",
    "WebFetchDocument",
    "WebFetchIntrospectionProvider",
    "WebFetchLink",
    "WebFetchModuleSnapshot",
    "WebFetchProviderModel",
    "WebFetchProviderRepository",
    "WebFetchRequest",
    "WebFetchResult",
    "WebLayoutInspectionRequest",
    "WebLayoutInspectionResult",
    "WebFetchService",
    "WebScreenshotRequest",
    "WebScreenshotResult",
    "WebScreenshotTool",
    "inspect_web_fetch",
    "plain_http_fetch",
    "playwright_python_available",
    "register_with_core",
    "run_browser_service_cli",
]

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, create_model

from pal.execution.tool_facade import StrictToolModel, StructuredToolOutput


def _strict_model(name: str, fields: dict[str, tuple[Any, Any]]):
    return create_model(name, __base__=StrictToolModel, **fields)


BrowserNavigateInput = _strict_model(
    "BrowserNavigateInput",
    {"url": (str, Field(...)), "timeout_ms": (int, Field(60000))},
)
BrowserReadInput = _strict_model(
    "BrowserReadInput",
    {
        "url": (str | None, Field(None)),
        "timeout_ms": (int, Field(60000)),
        "max_chars": (int, Field(12000)),
        "max_links": (int, Field(80)),
    },
)
BrowserSnapshotInput = _strict_model(
    "BrowserSnapshotInput",
    {
        "target": (str | None, Field(None)),
        "depth": (int | None, Field(8)),
        "boxes": (bool, Field(False)),
        "max_chars": (int, Field(12000)),
        "timeout_ms": (int, Field(15000)),
    },
)
BrowserFindInput = _strict_model(
    "BrowserFindInput",
    {
        "text": (str | None, Field(None)),
        "regex": (str | None, Field(None)),
        "timeout_ms": (int, Field(15000)),
    },
)
BrowserClickInput = _strict_model(
    "BrowserClickInput",
    {
        "target": (str, Field(...)),
        "button": (Literal["left", "right", "middle"], Field("left")),
        "modifiers": (list[str], Field(default_factory=list)),
        "double": (bool, Field(False)),
        "timeout_ms": (int, Field(15000)),
    },
)
BrowserFillInput = _strict_model(
    "BrowserFillInput",
    {
        "target": (str, Field(...)),
        "text": (str, Field(...)),
        "submit": (bool, Field(False)),
        "timeout_ms": (int, Field(15000)),
    },
)
BrowserTypeInput = _strict_model(
    "BrowserTypeInput",
    {"text": (str, Field(...)), "timeout_ms": (int, Field(15000))},
)
BrowserPressInput = _strict_model(
    "BrowserPressInput",
    {"key": (str, Field(...)), "timeout_ms": (int, Field(15000))},
)
BrowserTargetInput = _strict_model(
    "BrowserTargetInput",
    {"target": (str, Field(...)), "timeout_ms": (int, Field(15000))},
)
BrowserSelectInput = _strict_model(
    "BrowserSelectInput",
    {
        "target": (str, Field(...)),
        "value": (str, Field(...)),
        "timeout_ms": (int, Field(15000)),
    },
)
BrowserCheckInput = _strict_model(
    "BrowserCheckInput",
    {
        "target": (str, Field(...)),
        "checked": (bool, Field(True)),
        "timeout_ms": (int, Field(15000)),
    },
)
BrowserScrollInput = _strict_model(
    "BrowserScrollInput",
    {"dx": (int, Field(0)), "dy": (int, Field(...)), "timeout_ms": (int, Field(15000))},
)
BrowserResizeInput = _strict_model(
    "BrowserResizeInput",
    {
        "width": (int, Field(...)),
        "height": (int, Field(...)),
        "timeout_ms": (int, Field(15000)),
    },
)
BrowserHistoryInput = _strict_model(
    "BrowserHistoryInput",
    {
        "operation": (Literal["back", "forward", "reload"], Field(...)),
        "timeout_ms": (int, Field(60000)),
    },
)
BrowserTabsInput = _strict_model(
    "BrowserTabsInput",
    {
        "operation": (Literal["list", "new", "select", "close"], Field("list")),
        "index": (int | None, Field(None)),
        "url": (str | None, Field(None)),
        "timeout_ms": (int, Field(60000)),
    },
)
BrowserDialogInput = _strict_model(
    "BrowserDialogInput",
    {
        "operation": (Literal["accept", "dismiss"], Field(...)),
        "prompt": (str | None, Field(None)),
        "timeout_ms": (int, Field(15000)),
    },
)
BrowserInspectLayoutInput = _strict_model(
    "BrowserInspectLayoutInput",
    {
        "selector": (str, Field(...)),
        "max_elements": (int, Field(20)),
        "timeout_ms": (int, Field(15000)),
    },
)
BrowserScreenshotInput = _strict_model(
    "BrowserScreenshotInput",
    {
        "target": (str | None, Field(None)),
        "full_page": (bool, Field(False)),
        "hires": (bool, Field(False)),
        "timeout_ms": (int, Field(30000)),
    },
)
BrowserResetInput = _strict_model(
    "BrowserResetInput",
    {"confirm": (Literal[True], Field(...))},
)
BrowserActionOutput = StructuredToolOutput


__all__ = [
    "BrowserActionOutput",
    "BrowserCheckInput",
    "BrowserClickInput",
    "BrowserDialogInput",
    "BrowserFillInput",
    "BrowserFindInput",
    "BrowserHistoryInput",
    "BrowserInspectLayoutInput",
    "BrowserNavigateInput",
    "BrowserPressInput",
    "BrowserReadInput",
    "BrowserResetInput",
    "BrowserResizeInput",
    "BrowserScreenshotInput",
    "BrowserScrollInput",
    "BrowserSelectInput",
    "BrowserSnapshotInput",
    "BrowserTabsInput",
    "BrowserTargetInput",
    "BrowserTypeInput",
]

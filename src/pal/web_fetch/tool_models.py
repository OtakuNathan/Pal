from __future__ import annotations

from typing import Any, Literal

from pydantic import ConfigDict, Field, create_model, model_validator

from pal.execution.tool_facade import StrictToolModel, StructuredToolOutput


TARGET_DESCRIPTION = (
    "Current snapshot ref such as e15, or a unique CSS selector (#main > button.submit) "
    "or Playwright locator (getByRole('button', { name: 'Submit' })). "
    "Obtain refs from browser_snapshot/browser_find on the current page; refresh after navigation or stale-ref errors."
)


def _strict_model(name: str, fields: dict[str, tuple[Any, Any]]):
    return create_model(name, __base__=StrictToolModel, **fields)


BrowserNavigateInput = _strict_model(
    "BrowserNavigateInput",
    {
        "url": (str, Field(..., max_length=8192)),
        "timeout_ms": (int, Field(60000, ge=1000, le=120000, description="Timeout in milliseconds.")),
        "max_chars": (int, Field(12000, ge=1000, le=100000, description="Inline page-text preview budget; the complete captured text is saved to text_file for rg/read_file.")),
        "max_links": (int, Field(80, ge=0, le=500, description="Maximum inline links; the complete captured list is saved to links_file. Zero skips link collection.")),
    },
)
BrowserReadInput = _strict_model(
    "BrowserReadInput",
    {
        "url": (str | None, Field(None, max_length=8192)),
        "timeout_ms": (int, Field(60000, ge=1000, le=120000, description="Timeout in milliseconds.")),
        "max_chars": (int, Field(12000, ge=1000, le=100000, description="Inline page-text preview budget; the complete captured text is saved to text_file for rg/read_file.")),
        "max_links": (int, Field(80, ge=0, le=500, description="Maximum inline links; the complete captured list is saved to links_file. Zero skips link collection.")),
    },
)
BrowserSnapshotInput = _strict_model(
    "BrowserSnapshotInput",
    {
        "target": (str | None, Field(None, max_length=500, description=TARGET_DESCRIPTION)),
        "depth": (int | None, Field(8, ge=1, le=30)),
        "boxes": (bool, Field(False)),
        "max_chars": (int, Field(12000, ge=1000, le=100000, description="Inline preview budget; captured output is saved to text_file for rg/read_file.")),
        "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds.")),
    },
)
class BrowserFindInput(StrictToolModel):
    model_config = ConfigDict(strict=True, extra="forbid", json_schema_extra={
        "oneOf": [
            {"required": ["text"], "properties": {"text": {"type": "string", "minLength": 1}, "regex": {"type": "null"}}},
            {"required": ["regex"], "properties": {"regex": {"type": "string", "minLength": 1}, "text": {"type": "null"}}},
        ]})
    text: str | None = Field(None, min_length=1, max_length=500, description="Literal text; supply exactly one of text or regex.")
    regex: str | None = Field(None, min_length=1, max_length=500, description="Regular expression; supply exactly one of text or regex.")
    timeout_ms: int = Field(15000, ge=1000, le=120000)

    @model_validator(mode="after")
    def validate_query(self):
        if bool(self.text) == bool(self.regex):
            raise ValueError("Supply exactly one nonempty text or regex.")
        return self


BrowserClickInput = _strict_model(
    "BrowserClickInput",
    {
        "target": (str, Field(..., max_length=500, description=TARGET_DESCRIPTION)),
        "button": (Literal["left", "right", "middle"], Field("left")),
        "modifiers": (list[Literal["Alt", "Control", "ControlOrMeta", "Meta", "Shift"]], Field(default_factory=list, description="Modifier keys held during the click.")),
        "double": (bool, Field(False)),
        "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds.")),
    },
)
BrowserFillInput = _strict_model(
    "BrowserFillInput",
    {
        "target": (str, Field(..., max_length=500, description=TARGET_DESCRIPTION)),
        "text": (str, Field(..., max_length=20000)),
        "submit": (bool, Field(False)),
        "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds.")),
    },
)
BrowserTypeInput = _strict_model(
    "BrowserTypeInput",
    {"text": (str, Field(..., max_length=20000)), "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds."))},
)
BrowserPressInput = _strict_model(
    "BrowserPressInput",
    {"key": (str, Field(..., max_length=80, description="Playwright key or chord, e.g. Enter, ArrowLeft, a, Control+a. Acts on the currently focused element.")), "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds."))},
)
BrowserTargetInput = _strict_model(
    "BrowserTargetInput",
    {"target": (str, Field(..., max_length=500, description=TARGET_DESCRIPTION)), "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds."))},
)
BrowserSelectInput = _strict_model(
    "BrowserSelectInput",
    {
        "target": (str, Field(..., max_length=500, description=TARGET_DESCRIPTION)),
        "value": (str, Field(..., max_length=1000, description="HTML option value from the inspected dropdown, not its index or assumed display label.")),
        "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds.")),
    },
)
BrowserCheckInput = _strict_model(
    "BrowserCheckInput",
    {
        "target": (str, Field(..., max_length=500, description=TARGET_DESCRIPTION)),
        "checked": (bool, Field(True)),
        "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds.")),
    },
)
BrowserScrollInput = _strict_model(
    "BrowserScrollInput",
    {"dx": (int, Field(0, ge=-100000, le=100000)), "dy": (int, Field(..., ge=-100000, le=100000)), "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds."))},
)
BrowserResizeInput = _strict_model(
    "BrowserResizeInput",
    {
        "width": (int, Field(..., ge=320, le=4096)),
        "height": (int, Field(..., ge=320, le=4096)),
        "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds.")),
    },
)
BrowserHistoryInput = _strict_model(
    "BrowserHistoryInput",
    {
        "operation": (Literal["back", "forward", "reload"], Field(...)),
        "timeout_ms": (int, Field(60000, ge=1000, le=120000, description="Timeout in milliseconds.")),
    },
)
class BrowserTabsInput(StrictToolModel):
    model_config = ConfigDict(strict=True, extra="forbid", json_schema_extra={
        "if": {"properties": {"operation": {"const": "select"}}, "required": ["operation"]},
        "then": {"required": ["index"], "properties": {"index": {"type": "integer", "minimum": 0}}},
    })
    operation: Literal["list", "new", "select", "close"] = "list"
    index: int | None = Field(None, ge=0, description="Zero-based index from browser_tabs list; required for select. Omit for close to close the current tab.")
    url: str | None = Field(None, max_length=8192)
    timeout_ms: int = Field(60000, ge=1000, le=120000)

    @model_validator(mode="after")
    def validate_operation(self):
        if self.operation == "select" and self.index is None:
            raise ValueError("index is required for select; copy it from browser_tabs list.")
        return self


BrowserDialogInput = _strict_model(
    "BrowserDialogInput",
    {
        "operation": (Literal["accept", "dismiss"], Field(...)),
        "prompt": (str | None, Field(None, max_length=4000)),
        "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds.")),
    },
)
BrowserEvaluateInput = _strict_model(
    "BrowserEvaluateInput",
    {
        "func": (str, Field(..., max_length=20000)),
        "target": (str | None, Field(None, max_length=500, description=TARGET_DESCRIPTION)),
        "max_chars": (int, Field(20000, ge=200, le=100000, description="Inline preview budget. Oversized objects/arrays return a JSON preview and original result_type; text_file preserves the complete result. Read that file instead of rerunning the script.")),
        "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds.")),
    },
)
BrowserNetworkInput = _strict_model(
    "BrowserNetworkInput",
    {
        "operation": (Literal["start", "read", "clear"], Field("read")),
        "url_filter": (str | None, Field(None, max_length=500)),
        "since": (int, Field(0, ge=0, description="Exclusive sequence cursor: pass next_since from the previous read. Reset to 0 after navigation.")),
        "limit": (int, Field(50, ge=1, le=200)),
        "clear_on_read": (bool, Field(False, description="Discard the scanned prefix through next_since, including nonmatching entries; preserve subsequent pages.")),
        "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds.")),
    },
)
BrowserInspectLayoutInput = _strict_model(
    "BrowserInspectLayoutInput",
    {
        "selector": (str, Field(..., max_length=500)),
        "max_elements": (int, Field(20, ge=1, le=20, description="Inline element preview limit; text_file contains the full captured layout for the selector.")),
        "timeout_ms": (int, Field(15000, ge=1000, le=120000, description="Timeout in milliseconds.")),
    },
)
BrowserScreenshotInput = _strict_model(
    "BrowserScreenshotInput",
    {
        "target": (str | None, Field(None, max_length=500, description=TARGET_DESCRIPTION)),
        "full_page": (bool, Field(False)),
        "hires": (bool, Field(False)),
        "timeout_ms": (int, Field(30000, ge=1000, le=120000, description="Timeout in milliseconds.")),
    },
)
BrowserResetInput = _strict_model(
    "BrowserResetInput",
    {"confirm": (Literal[True], Field(...))},
)
class BrowserExtensionManageInput(StrictToolModel):
    model_config = ConfigDict(strict=True, extra="forbid", json_schema_extra={
        "allOf": [
            {"if": {"properties": {"operation": {"const": "mount"}}},
             "then": {"required": ["path"], "properties": {"path": {"type": "string", "minLength": 1, "pattern": r"\S"}}}},
            {"if": {"properties": {"operation": {"enum": ["reload", "unmount"]}}},
             "then": {"required": ["extension_id"], "properties": {"extension_id": {"type": "string", "minLength": 1, "pattern": r"\S"}}}},
        ]})
    operation: Literal["mount", "unmount", "reload"]
    path: str | None = Field(None, description="Required for mount: extension directory path.")
    extension_id: str | None = Field(None, description="Required for reload/unmount: ID returned by extension management.")
    timeout_ms: int = Field(20000, ge=1000, le=120000)

    @model_validator(mode="after")
    def validate_operation(self):
        field = "path" if self.operation == "mount" else "extension_id"
        if not (getattr(self, field) or "").strip():
            raise ValueError(f"{field} is required for {self.operation}.")
        return self


BrowserActionOutput = StructuredToolOutput


__all__ = [
    "BrowserActionOutput",
    "BrowserExtensionManageInput",
    "BrowserCheckInput",
    "BrowserClickInput",
    "BrowserDialogInput",
    "BrowserEvaluateInput",
    "BrowserFillInput",
    "BrowserFindInput",
    "BrowserHistoryInput",
    "BrowserInspectLayoutInput",
    "BrowserNavigateInput",
    "BrowserNetworkInput",
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

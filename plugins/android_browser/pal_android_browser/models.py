from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from pal.execution.tool_facade import StrictToolModel

Selector = Literal["text", "textContains", "textMatches", "id", "desc", "descContains", "descMatches", "class"]


class DeviceInput(StrictToolModel):
    device: str = Field(default="_", min_length=1, description="Paired device id/name; _ requires exactly one online phone.")


class EmptyInput(StrictToolModel):
    pass


class ObserveInput(DeviceInput):
    all_windows: bool = False


class ScreenshotInput(DeviceInput):
    max_edge: int = Field(default=1920, ge=256, le=1920,
                          description="Maximum image edge. Default preserves the native viewport so image coordinates match taps.")


class ScreenshotOutput(StrictToolModel):
    device: str
    package: str
    activity: str
    workspace: Literal["pal"]
    generation: int
    width: int
    height: int
    viewport_width: int
    viewport_height: int
    frame_age_ms: int
    sha256: str
    path: str
    artifact_id: str | None = None
    registration: Literal["registered", "unavailable", "failed", "unknown"]
    registration_error: str | None = None


class QueryInput(DeviceInput):
    by: Selector
    value: str


class Target(StrictToolModel):
    by: Selector
    value: str
    nth: int = Field(default=0, ge=0, description="Explicit zero-based match index. On Pal Android, omission requires a unique match; query duplicates first.")
    timeout: float = Field(default=5.0, ge=0, le=10)


class TouchStep(Target):
    cmd: Literal["touch"]


class InputStep(Target):
    cmd: Literal["input"]
    text: str
    append: bool = False


class FocusInputStep(StrictToolModel):
    cmd: Literal["input"]
    by: Literal["focus"]
    package: str = Field(min_length=1, pattern=r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$", description="Expected app package from android_focus.")
    editor: str = Field(min_length=1, max_length=64, description="Opaque editor lease from android_focus; expires on focus changes or input restart.")
    text: str = Field(min_length=1, max_length=16384, description="Insert at the editor cursor/selection. Does not replace the entire field or press Enter.")


InputAction = Annotated[InputStep | FocusInputStep, Field(discriminator="by")]


class TapStep(StrictToolModel):
    cmd: Literal["tap"]
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class SwipeStep(StrictToolModel):
    cmd: Literal["swipe"]
    x1: Literal["up", "down", "left", "right"]
    ms: int = Field(default=300, ge=100, le=2000)


class NavigationStep(StrictToolModel):
    cmd: Literal["back", "home", "recents"]


class LaunchStep(StrictToolModel):
    cmd: Literal["launch"]
    package: str = Field(min_length=1)


class WaitStep(StrictToolModel):
    cmd: Literal["wait_idle"]
    ms: int = Field(default=500, ge=100, le=2000)
    timeout: float = Field(default=5.0, ge=0.1, le=10)


Step = Annotated[TouchStep | InputAction | TapStep | SwipeStep | NavigationStep | LaunchStep | WaitStep,
                 Field(discriminator="cmd")]


class ActInput(DeviceInput):
    steps: list[Step] = Field(min_length=1, max_length=8,
                             description="Known UI primitives in order. Stops at first error; returns a fresh snapshot.")


class DevicesOutput(StrictToolModel):
    devices: list[dict[str, Any]]


class AppsOutput(StrictToolModel):
    apps: list[dict[str, Any]]


class ScreenOutput(StrictToolModel):
    package: str
    activity: str
    workspace: str | None = None
    generation: int | None = None
    truncated: bool = False
    width: int
    height: int
    tree: dict[str, Any] | list[Any] | None


class QueryOutput(StrictToolModel):
    matches: list[dict[str, Any]]


class ActOutput(StrictToolModel):
    results: list[dict[str, Any]]
    screen: dict[str, Any] | None = None
    snapshot_error: str | dict[str, Any] | None = None


class ReadyFocus(StrictToolModel):
    kind: Literal["ready"]
    editor: str
    package: str
    field_id: int


class UnavailableFocus(StrictToolModel):
    kind: Literal["unavailable"]
    fault: dict[str, str]


class FocusOutput(StrictToolModel):
    focus: Annotated[ReadyFocus | UnavailableFocus, Field(discriminator="kind")]

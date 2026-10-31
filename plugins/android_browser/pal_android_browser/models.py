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


class QueryInput(DeviceInput):
    by: Selector
    value: str


class Target(StrictToolModel):
    by: Selector
    value: str
    nth: int = Field(default=0, ge=0, description="Zero-based match index; query first if the selector is ambiguous.")
    timeout: float = Field(default=5.0, ge=0, le=10)


class TouchStep(Target):
    cmd: Literal["touch"]


class InputStep(Target):
    cmd: Literal["input"]
    text: str
    append: bool = False


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


Step = Annotated[TouchStep | InputStep | TapStep | SwipeStep | NavigationStep | LaunchStep | WaitStep,
                 Field(discriminator="cmd")]


class ActInput(DeviceInput):
    steps: list[Step] = Field(min_length=1, max_length=8,
                             description="Known UI primitives in order. Stops at first error; returns a fresh snapshot.")


class DevicesOutput(StrictToolModel):
    devices: list[dict[str, Any]]


class ScreenOutput(StrictToolModel):
    package: str
    activity: str
    width: int
    height: int
    tree: dict[str, Any] | list[Any] | None


class QueryOutput(StrictToolModel):
    matches: list[dict[str, Any]]


class ActOutput(StrictToolModel):
    results: list[dict[str, Any]]
    screen: dict[str, Any] | None = None
    snapshot_error: str | None = None

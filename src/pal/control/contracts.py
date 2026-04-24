from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from pal.foundation.persistence import utc_now


@dataclass(frozen=True)
class ControlRoute:
    endpoint_id: str
    channel_kind: str
    reply_target: dict[str, Any] = field(default_factory=dict)
    control_scope_key: str = ""
    correlation_id: str | None = None


@dataclass(frozen=True)
class ControlCommandInvocation:
    command_name: str
    argv: tuple[str, ...] = ()
    raw_text: str = ""
    route: ControlRoute | None = None
    source_kind: str = ""
    origin_event_id: str = ""


@dataclass(frozen=True)
class ControlAction:
    action_kind: str
    target_scope: str
    target_id: str | None = None
    requires_user_confirmation: bool = False
    args: dict[str, Any] = field(default_factory=dict)
    route: ControlRoute | None = None
    notes: str = ""


@dataclass(frozen=True)
class InteractionButtonSpec:
    label: str
    action_key: str
    action_args: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class InteractionMessageSpec:
    interaction_id: str
    interaction_kind: str
    route: ControlRoute | None = None
    text: str = ""
    buttons: tuple[tuple[InteractionButtonSpec, ...], ...] = ()
    expires_at: str | None = None


@dataclass(frozen=True)
class InteractionResult:
    interaction_id: str
    interaction_kind: str
    action_key: str
    action_args: dict[str, Any] = field(default_factory=dict)
    route: ControlRoute | None = None


ControlCommandHandler = Callable[[ControlCommandInvocation], ControlAction | None]


@dataclass(frozen=True)
class ControlCommandSpec:
    name: str
    handler: ControlCommandHandler
    aliases: tuple[str, ...] = ()
    description: str = ""
    usage: str = ""
    show_in_panel: bool = False
    panel_group: str = "builtin"
    panel_button: bool = False
    panel_label: str = ""
    interaction_action_key: str = ""
    confirm_policy: str = "none"


@dataclass(frozen=True)
class ControlEvent:
    event_kind: str
    source_kind: str
    payload: dict[str, Any]
    route: ControlRoute | None = None
    response_handle: dict[str, Any] | None = None
    correlation_id: str | None = None
    created_at: str = field(default_factory=utc_now)
    event_id: str = field(default_factory=lambda: str(uuid4()))


class ControlPlanePort(Protocol):
    def parse_event(self, event: ControlEvent) -> ControlAction | None:
        ...

    def handle_interaction(self, result: InteractionResult) -> ControlAction | None:
        ...

    def register_command(self, spec: ControlCommandSpec) -> None:
        ...

    def unregister_command(self, name: str) -> None:
        ...

    def list_panel_commands(self) -> list[ControlCommandSpec]:
        ...

    def render_panel_text(self) -> str:
        ...

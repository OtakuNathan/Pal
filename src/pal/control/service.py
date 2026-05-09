from __future__ import annotations

import shlex
from dataclasses import dataclass, field

from pal.control.contracts import (
    ControlAction,
    ControlCommandInvocation,
    ControlCommandSpec,
    ControlEvent,
    ControlRoute,
    InteractionResult,
    ControlPlanePort,
)
from pal.shared import EventKind

_THINK_ALIASES = {
    "off": "off",
    "low": "low",
    "balanced": "balanced",
    "deep": "deep",
    "medium": "balanced",
    "high": "deep",
}


@dataclass
class ControlCommandRegistry:
    commands: dict[str, ControlCommandSpec] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)

    def register(self, spec: ControlCommandSpec) -> None:
        normalized = _normalize_command_name(spec.name)
        if not normalized:
            raise ValueError("control command name cannot be empty")
        if normalized in self.commands or normalized in self.aliases:
            raise ValueError(f"control command already registered: {normalized}")
        normalized_aliases: list[str] = []
        for alias in spec.aliases:
            normalized_alias = _normalize_command_name(alias)
            if not normalized_alias:
                continue
            if normalized_alias == normalized:
                continue
            if normalized_alias in self.commands or normalized_alias in self.aliases:
                raise ValueError(f"control command alias already registered: {normalized_alias}")
            normalized_aliases.append(normalized_alias)
        normalized_spec = ControlCommandSpec(
            name=normalized,
            handler=spec.handler,
            aliases=tuple(normalized_aliases),
            description=spec.description,
            usage=spec.usage or f"/{normalized}",
            show_in_panel=spec.show_in_panel,
            panel_group=spec.panel_group,
            panel_button=spec.panel_button,
            panel_label=spec.panel_label or spec.name,
            interaction_action_key=spec.interaction_action_key,
            confirm_policy=spec.confirm_policy,
        )
        self.commands[normalized] = normalized_spec
        for alias in normalized_aliases:
            self.aliases[alias] = normalized

    def unregister(self, name: str) -> None:
        normalized = _normalize_command_name(name)
        canonical = self.aliases.pop(normalized, normalized)
        spec = self.commands.pop(canonical, None)
        if spec is None:
            return
        for alias in spec.aliases:
            self.aliases.pop(alias, None)

    def resolve(self, name: str) -> ControlCommandSpec | None:
        normalized = _normalize_command_name(name)
        if normalized in self.commands:
            return self.commands[normalized]
        canonical = self.aliases.get(normalized)
        if canonical is None:
            return None
        return self.commands.get(canonical)

    def list_panel_commands(self) -> list[ControlCommandSpec]:
        return [spec for spec in self.commands.values() if spec.show_in_panel]


@dataclass
class ControlPlane(ControlPlanePort):
    registry: ControlCommandRegistry = field(default_factory=ControlCommandRegistry)

    def __post_init__(self) -> None:
        self._register_builtins()

    def register_command(self, spec: ControlCommandSpec) -> None:
        self.registry.register(spec)

    def unregister_command(self, name: str) -> None:
        self.registry.unregister(name)

    def list_panel_commands(self) -> list[ControlCommandSpec]:
        return self.registry.list_panel_commands()

    def render_panel_text(self) -> str:
        lines = [
            "Pal Control Panel",
            "",
            "Commands:",
        ]
        for spec in self.list_panel_commands():
            usage = spec.usage or f"/{spec.name}"
            description = spec.description or "No description."
            lines.append(f"{usage} - {description}")
        return "\n".join(lines).strip()

    def parse_event(self, event: ControlEvent) -> ControlAction | None:
        if event.event_kind != EventKind.SLASH_COMMAND:
            return None
        invocation = self._parse_invocation(event)
        if invocation is None:
            return None
        spec = self.registry.resolve(invocation.command_name)
        if spec is None:
            return ControlAction(
                action_kind="unknown_command",
                target_scope="control",
                route=invocation.route,
                args={
                    "command_name": invocation.command_name,
                    "raw_text": invocation.raw_text,
                },
                notes=f"Unknown command: /{invocation.command_name}",
            )
        action = spec.handler(invocation)
        if action is None:
            return None
        if action.route is not None:
            return action
        return ControlAction(
            action_kind=action.action_kind,
            target_scope=action.target_scope,
            target_id=action.target_id,
            requires_user_confirmation=action.requires_user_confirmation,
            args=dict(action.args),
            route=invocation.route,
            notes=action.notes,
        )

    def handle_interaction(self, result: InteractionResult) -> ControlAction | None:
        action_key = str(result.action_key or "").strip()
        if not action_key:
            return _with_interaction_context(
                ControlAction(
                    action_kind="invalid_command",
                    target_scope="control",
                    route=result.route,
                    args={"reason": "empty interaction action"},
                    notes="Unsupported interaction action.",
                ),
                result,
            )
        if action_key in {"control.panel.show", "control.panel.back"}:
            action = ControlAction(
                action_kind="show_panel",
                target_scope="control",
                route=result.route,
            )
            return _with_interaction_context(action, result)
        if action_key == "control.think.open":
            action = ControlAction(
                action_kind="show_think",
                target_scope="runtime",
                route=result.route,
            )
            return _with_interaction_context(action, result)
        if action_key == "control.log.open":
            action = ControlAction(
                action_kind="show_log",
                target_scope="runtime",
                route=result.route,
            )
            return _with_interaction_context(action, result)
        if action_key == "control.think.set":
            requested = _normalize_think_level(str(result.action_args.get("think_level") or ""))
            if requested is None:
                action = ControlAction(
                    action_kind="invalid_command",
                    target_scope="control",
                    route=result.route,
                    args={"reason": "invalid think level"},
                    notes="Valid think levels: off, low, balanced, deep.",
                )
                return _with_interaction_context(action, result)
            action = ControlAction(
                action_kind="set_think",
                target_scope="runtime",
                route=result.route,
                args={"think_level": requested},
            )
            return _with_interaction_context(action, result)
        if action_key in {"control.log.start", "control.log.end"}:
            enabled = action_key == "control.log.start"
            action = ControlAction(
                action_kind="set_log",
                target_scope="runtime",
                route=result.route,
                args={"prompt_log_enabled": enabled},
            )
            return _with_interaction_context(action, result)
        if action_key == "control.interrupt.run":
            action = ControlAction(
                action_kind="interrupt_turn",
                target_scope="runtime",
                route=result.route,
            )
            return _with_interaction_context(action, result)
        if action_key == "control.compact.run":
            action = ControlAction(
                action_kind="compact_memory",
                target_scope="memory",
                route=result.route,
            )
            return _with_interaction_context(action, result)
        if action_key == "control.reset.open":
            action = ControlAction(
                action_kind="open_reset_confirm",
                target_scope="memory",
                route=result.route,
            )
            return _with_interaction_context(action, result)
        if action_key == "control.reset.confirm":
            request_id = str(result.action_args.get("request_id") or result.interaction_id).strip()
            action = ControlAction(
                action_kind="reset_memory",
                target_scope="memory",
                route=result.route,
                args={"request_id": request_id},
            )
            return _with_interaction_context(action, result)
        if action_key == "control.reset.cancel":
            request_id = str(result.action_args.get("request_id") or result.interaction_id).strip()
            action = ControlAction(
                action_kind="cancel_reset_confirm",
                target_scope="memory",
                route=result.route,
                args={"request_id": request_id},
            )
            return _with_interaction_context(action, result)
        if action_key == "control.command.run":
            command_name = _normalize_command_name(str(result.action_args.get("command_name") or ""))
            if not command_name:
                return _with_interaction_context(
                    ControlAction(
                        action_kind="invalid_command",
                        target_scope="control",
                        route=result.route,
                        args={"reason": "missing command_name"},
                        notes="Missing control command target.",
                    ),
                    result,
                )
            spec = self.registry.resolve(command_name)
            if spec is None:
                action = ControlAction(
                    action_kind="unknown_command",
                    target_scope="control",
                    route=result.route,
                    args={"command_name": command_name},
                    notes=f"Unknown command: /{command_name}",
                )
                return _with_interaction_context(action, result)
            invocation = ControlCommandInvocation(
                command_name=spec.name,
                argv=(),
                raw_text=f"/{spec.name}",
                route=result.route,
                source_kind="interaction",
                origin_event_id=result.interaction_id,
            )
            action = spec.handler(invocation)
            if action is None:
                return _with_interaction_context(
                    ControlAction(
                        action_kind="invalid_command",
                        target_scope="control",
                        route=result.route,
                        args={"reason": "interaction handler returned no action", "command_name": command_name},
                        notes=f"Control command /{command_name} did not produce an action.",
                    ),
                    result,
                )
            return _with_interaction_context(action, result)
        if action_key == "control.action.dispatch":
            action = _action_from_payload(result.action_args, route=result.route)
            if action is None:
                return _with_interaction_context(
                    ControlAction(
                        action_kind="invalid_command",
                        target_scope="control",
                        route=result.route,
                        args={"reason": "invalid dispatched action"},
                        notes="Unsupported interaction action.",
                    ),
                    result,
                )
            return _with_interaction_context(action, result)
        return _with_interaction_context(
            ControlAction(
                action_kind="invalid_command",
                target_scope="control",
                route=result.route,
                args={"reason": "unsupported interaction action", "action_key": action_key},
                notes=f"Unsupported interaction action: {action_key}",
            ),
            result,
        )

    def _parse_invocation(self, event: ControlEvent) -> ControlCommandInvocation | None:
        payload = dict(event.payload or {})
        command_name = str(payload.get("command_name") or payload.get("command") or "").strip()
        argv_payload = payload.get("argv")
        if argv_payload is not None and command_name:
            argv = tuple(str(value).strip() for value in list(argv_payload or []) if str(value).strip())
            raw_text = str(payload.get("text") or payload.get("raw_text") or f"/{command_name} {' '.join(argv)}").strip()
            return ControlCommandInvocation(
                command_name=_normalize_command_name(command_name),
                argv=argv,
                raw_text=raw_text,
                route=event.route,
                source_kind=event.source_kind,
                origin_event_id=event.event_id,
            )
        raw_text = str(payload.get("text") or command_name).strip()
        if not raw_text:
            return None
        if raw_text.startswith("/"):
            raw = raw_text[1:]
        else:
            raw = raw_text
        try:
            parts = shlex.split(raw)
        except ValueError:
            parts = raw.split()
        if not parts:
            return None
        return ControlCommandInvocation(
            command_name=_normalize_command_name(parts[0]),
            argv=tuple(str(item).strip() for item in parts[1:] if str(item).strip()),
            raw_text=raw_text,
            route=event.route,
            source_kind=event.source_kind,
            origin_event_id=event.event_id,
        )

    def _register_builtins(self) -> None:
        self.register_command(
            ControlCommandSpec(
                name="control",
                handler=self._handle_control,
                description="Show the control panel and command help.",
                usage="/control",
                show_in_panel=True,
                panel_group="builtin",
                panel_button=False,
                panel_label="Control",
            )
        )
        self.register_command(
            ControlCommandSpec(
                name="think",
                handler=self._handle_think,
                description="Show or update the think level for future turns.",
                usage="/think [off|low|balanced|deep]",
                show_in_panel=True,
                panel_group="builtin",
                panel_button=True,
                panel_label="Think",
                interaction_action_key="control.think.open",
            )
        )
        self.register_command(
            ControlCommandSpec(
                name="interrupt",
                handler=self._handle_interrupt,
                description="Interrupt the current active turn in this scope.",
                usage="/interrupt",
                show_in_panel=True,
                panel_group="builtin",
                panel_button=True,
                panel_label="Interrupt",
                interaction_action_key="control.interrupt.run",
            )
        )
        self.register_command(
            ControlCommandSpec(
                name="log",
                handler=self._handle_log,
                description="Show or update prompt debug logging for future turns.",
                usage="/log [start|end]",
                show_in_panel=True,
                panel_group="builtin",
                panel_button=True,
                panel_label="Log",
                interaction_action_key="control.log.open",
            )
        )
        self.register_command(
            ControlCommandSpec(
                name="compact",
                handler=self._handle_compact,
                description="Manually trigger memory compaction.",
                usage="/compact",
                show_in_panel=True,
                panel_group="builtin",
                panel_button=True,
                panel_label="Compact",
                interaction_action_key="control.compact.run",
            )
        )
        self.register_command(
            ControlCommandSpec(
                name="reset",
                handler=self._handle_reset,
                description="Open memory reset confirmation for this scope.",
                usage="/reset",
                show_in_panel=True,
                panel_group="builtin",
                panel_button=True,
                panel_label="Reset Memory",
                interaction_action_key="control.reset.open",
                confirm_policy="confirm",
            )
        )
        for legacy in ("pause", "resume", "cancel", "approve"):
            self.register_command(
                ControlCommandSpec(
                    name=legacy,
                    handler=self._make_legacy_handler(legacy),
                    description=f"Legacy control command: {legacy}.",
                    usage=f"/{legacy}",
                    show_in_panel=False,
                )
            )

    def _handle_control(self, invocation: ControlCommandInvocation) -> ControlAction:
        return ControlAction(
            action_kind="show_panel",
            target_scope="control",
            route=invocation.route,
        )

    def _handle_think(self, invocation: ControlCommandInvocation) -> ControlAction:
        if not invocation.argv:
            return ControlAction(
                action_kind="show_think",
                target_scope="runtime",
                route=invocation.route,
            )
        requested = _normalize_think_level(invocation.argv[0])
        if requested is None:
            return ControlAction(
                action_kind="invalid_command",
                target_scope="control",
                route=invocation.route,
                args={
                    "command_name": invocation.command_name,
                    "reason": "invalid think level",
                },
                notes="Valid think levels: off, low, balanced, deep.",
            )
        return ControlAction(
            action_kind="set_think",
            target_scope="runtime",
            route=invocation.route,
            args={"think_level": requested},
        )

    def _handle_log(self, invocation: ControlCommandInvocation) -> ControlAction:
        if not invocation.argv:
            return ControlAction(
                action_kind="show_log",
                target_scope="runtime",
                route=invocation.route,
            )
        subcommand = str(invocation.argv[0] or "").strip().lower()
        if subcommand not in {"start", "end"}:
            return ControlAction(
                action_kind="invalid_command",
                target_scope="control",
                route=invocation.route,
                args={
                    "command_name": invocation.command_name,
                    "reason": "invalid log subcommand",
                },
                notes="Use /log start or /log end.",
            )
        return ControlAction(
            action_kind="set_log",
            target_scope="runtime",
            route=invocation.route,
            args={"prompt_log_enabled": subcommand == "start"},
        )

    def _handle_interrupt(self, invocation: ControlCommandInvocation) -> ControlAction:
        return ControlAction(
            action_kind="interrupt_turn",
            target_scope="runtime",
            route=invocation.route,
        )

    def _handle_compact(self, invocation: ControlCommandInvocation) -> ControlAction:
        return ControlAction(
            action_kind="compact_memory",
            target_scope="memory",
            route=invocation.route,
        )

    def _handle_reset(self, invocation: ControlCommandInvocation) -> ControlAction:
        if not invocation.argv:
            return ControlAction(
                action_kind="open_reset_confirm",
                target_scope="memory",
                route=invocation.route,
            )
        if invocation.argv[0].lower() != "confirm":
            return ControlAction(
                action_kind="invalid_command",
                target_scope="control",
                route=invocation.route,
                args={
                    "command_name": invocation.command_name,
                    "reason": "invalid reset subcommand",
                },
                notes="Use /reset or /reset confirm <request_id>.",
            )
        request_id = str(invocation.argv[1]).strip() if len(invocation.argv) > 1 else ""
        if not request_id:
            return ControlAction(
                action_kind="invalid_command",
                target_scope="control",
                route=invocation.route,
                args={
                    "command_name": invocation.command_name,
                    "reason": "missing reset request id",
                },
                notes="Use /reset confirm <request_id>.",
            )
        return ControlAction(
            action_kind="reset_memory",
            target_scope="memory",
            route=invocation.route,
            args={"request_id": request_id},
        )

    def _make_legacy_handler(self, name: str):
        def _handler(invocation: ControlCommandInvocation) -> ControlAction:
            return ControlAction(
                action_kind=name,
                target_scope="pal",
                route=invocation.route,
            )

        return _handler


def _normalize_command_name(value: str) -> str:
    text = str(value or "").strip().lower()
    return text[1:] if text.startswith("/") else text


def _normalize_think_level(value: str) -> str | None:
    return _THINK_ALIASES.get(str(value or "").strip().lower())


def _route_from_payload(payload: object) -> ControlRoute | None:
    if not isinstance(payload, dict):
        return None
    endpoint_id = str(payload.get("endpoint_id") or "").strip()
    channel_kind = str(payload.get("channel_kind") or "").strip()
    if not endpoint_id or not channel_kind:
        return None
    return ControlRoute(
        endpoint_id=endpoint_id,
        channel_kind=channel_kind,
        reply_target=dict(payload.get("reply_target") or {}),
        control_scope_key=str(payload.get("control_scope_key") or ""),
        correlation_id=str(payload.get("correlation_id") or "") or None,
    )


def _with_interaction_context(action: ControlAction, result: InteractionResult) -> ControlAction:
    args = dict(action.args)
    args["interaction_origin"] = "button"
    args["interaction_id"] = result.interaction_id
    args["interaction_kind"] = result.interaction_kind
    return ControlAction(
        action_kind=action.action_kind,
        target_scope=action.target_scope,
        target_id=action.target_id,
        requires_user_confirmation=action.requires_user_confirmation,
        args=args,
        route=action.route or result.route,
        notes=action.notes,
    )


def _action_from_payload(payload: dict, *, route: ControlRoute | None) -> ControlAction | None:
    if not isinstance(payload, dict):
        return None
    action_kind = str(payload.get("action_kind") or "").strip()
    target_scope = str(payload.get("target_scope") or "control").strip()
    if not action_kind or not target_scope:
        return None
    args = payload.get("args")
    if not isinstance(args, dict):
        args = {
            key: value
            for key, value in payload.items()
            if key not in {"action_kind", "target_scope", "target_id", "requires_user_confirmation", "notes"}
        }
    return ControlAction(
        action_kind=action_kind,
        target_scope=target_scope,
        target_id=str(payload.get("target_id") or "") or None,
        requires_user_confirmation=bool(payload.get("requires_user_confirmation")),
        args=dict(args),
        route=route,
        notes=str(payload.get("notes") or ""),
    )

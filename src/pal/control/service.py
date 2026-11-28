from __future__ import annotations

import shlex
import re
from threading import RLock
from typing import Callable
from dataclasses import dataclass, field

from pal.control.contracts import (
    ControlAction,
    ControlCommandInvocation,
    ControlCommandSpec,
    ControlInteractionSpec,
    ControlEvent,
    ControlRoute,
    InteractionResult,
    ControlPlanePort,
)
from pal.shared import EventKind
from pal.control.presentation import markdown_literal

@dataclass
class ControlCommandRegistry:
    commands: dict[str, ControlCommandSpec] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    lock: RLock = field(default_factory=RLock, repr=False)

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
        with self.lock:
            return self._resolve(name)

    def _resolve(self, name: str) -> ControlCommandSpec | None:
        normalized = _normalize_command_name(name)
        if normalized in self.commands:
            return self.commands[normalized]
        canonical = self.aliases.get(normalized)
        if canonical is None:
            return None
        return self.commands.get(canonical)

    def list_panel_commands(self) -> list[ControlCommandSpec]:
        with self.lock:
            return [spec for spec in self.commands.values() if spec.show_in_panel]


@dataclass
class InstalledControlCommands:
    registry: ControlCommandRegistry
    owned: dict[str, ControlCommandSpec]
    changed: Callable[[], None]
    interaction_handlers: dict[str, ControlInteractionSpec]
    owned_interactions: dict[str, ControlInteractionSpec]

    def close(self) -> None:
        removed = False
        with self.registry.lock:
            for name, spec in self.owned_interactions.items():
                if self.interaction_handlers.get(name) is spec:
                    self.interaction_handlers.pop(name)
                    removed = True
            for name, spec in self.owned.items():
                if self.registry.commands.get(name) is spec:
                    self.registry.unregister(name)
                    removed = True
        if removed:
            self.changed()


@dataclass
class ControlPlane(ControlPlanePort):
    registry: ControlCommandRegistry = field(default_factory=ControlCommandRegistry)
    catalog_revision: int = 0
    on_ready: Callable[[], None] | None = field(default=None, repr=False)
    interaction_handlers: dict[str, ControlInteractionSpec] = field(default_factory=dict)


    def _catalog_changed(self) -> None:
        with self.registry.lock:
            self.catalog_revision += 1
            ready = self.on_ready
        if ready is not None:
            ready()

    def install_commands(self, owner: str, specs: tuple[ControlCommandSpec, ...], *,
                         interactions: tuple[ControlInteractionSpec, ...] = ()) -> InstalledControlCommands:
        """Validate the entire contribution before publishing any command."""
        if not owner or not isinstance(specs, tuple):
            raise ValueError("slash commands require an owner and an immutable declaration")
        with self.registry.lock:
            candidate = ControlCommandRegistry(dict(self.registry.commands), dict(self.registry.aliases))
            owned = {}
            owned_interactions = {}
            for spec in interactions:
                if not isinstance(spec, ControlInteractionSpec) or not callable(spec.handler):
                    raise TypeError("control interactions require ControlInteractionSpec with a callable parser")
                if spec.action_key in {"control.command.run", "control.action.dispatch"}:
                    raise ValueError(f"reserved control interaction: {spec.action_key}")
                if not spec.action_key or spec.action_key in self.interaction_handlers or spec.action_key in owned_interactions:
                    raise ValueError(f"control interaction already registered: {spec.action_key}")
                owned_interactions[spec.action_key] = spec
            for spec in specs:
                if not isinstance(spec, ControlCommandSpec) or not callable(spec.handler):
                    raise TypeError("plugin slash commands require ControlCommandSpec with a callable handler")
                names = (spec.name, *spec.aliases)
                if len(set(names)) != len(names) or any(re.fullmatch(r"[a-z0-9_]{1,32}", name) is None for name in names):
                    raise ValueError(f"invalid plugin slash command declaration for {owner}")
                if spec.show_in_panel and not 1 <= len(spec.description.strip()) <= 256:
                    raise ValueError("menu commands require a description of 1–256 characters")
                candidate.register(spec)
                owned[spec.name] = candidate.commands[spec.name]
            self.registry.commands = candidate.commands
            self.registry.aliases = candidate.aliases
            self.interaction_handlers.update(owned_interactions)
        if owned or owned_interactions:
            self._catalog_changed()
        return InstalledControlCommands(self.registry, owned, self._catalog_changed,
                                        self.interaction_handlers, owned_interactions)

    def list_panel_commands(self) -> list[ControlCommandSpec]:
        return self.registry.list_panel_commands()

    def render_panel_text(self) -> str:
        lines = [
            "# Pal Control Panel",
            "",
            "**Commands**",
        ]
        for spec in self.list_panel_commands():
            usage = spec.usage or f"/{spec.name}"
            description = spec.description or "No description."
            lines.append(f"- **{markdown_literal(usage)}** — {markdown_literal(description)}")
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
                action_kind="fallback_user_message",
                target_scope="control",
                route=invocation.route,
                args={
                    "command_name": invocation.command_name,
                    "raw_text": invocation.raw_text,
                },
                notes=f"No slash command matched: /{invocation.command_name}",
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
            delivery=action.delivery,
            notes=action.notes,
        )

    def handle_interaction(self, result: InteractionResult) -> ControlAction | None:
        key = result.action_key.strip()
        spec = self.interaction_handlers.get(key)
        if spec is not None:
            action = spec.handler(result)
            return _with_interaction_context(action, result) if action is not None else None
        if key == "control.command.run":
            command_name = _normalize_command_name(str(result.action_args.get("command_name") or ""))
            command = self.registry.resolve(command_name)
            action = command.handler(ControlCommandInvocation(
                command_name=command_name, argv=tuple(result.action_args.get("argv") or ()),
                route=result.route, source_kind="interaction")) if command is not None else None
            if action is not None:
                return _with_interaction_context(action, result)
        if key == "control.action.dispatch":
            action = _action_from_payload(dict(result.action_args), route=result.route)
            if action is not None:
                return _with_interaction_context(action, result)
        return _with_interaction_context(ControlAction(
            action_kind="invalid_command", target_scope="control", route=result.route,
            args={"reason": "unsupported interaction action", "action_key": key},
            notes=f"Unsupported interaction action: {key}"), result)

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


def _normalize_command_name(value: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("/"):
        text = text[1:]
    if "@" in text:
        text = text.split("@", 1)[0]
    return text


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
        delivery=action.delivery,
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

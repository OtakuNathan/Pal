"""Async interactive loop for the Pal socket TTY."""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rich.console import Console

from pal.tty.interactions import TtyInteraction
from pal.tty.render import AssistantBodyRenderer, OutputBoundary, TtyRenderer
from pal.tty.session import SocketDisconnected, SocketProtocolError, SocketSession


class TtyRepl:
    """One interactive session over a Pal Unix socket."""

    def __init__(
        self,
        socket_path: Path,
        *,
        open_unix_connection: Callable[[str], Awaitable[tuple[Any, Any]]],
        read_message: Callable[[Any], Awaitable[dict[str, Any]]],
        request_id_factory: Callable[[], str],
        prompt_session: Any | None = None,
        interaction_selector: (
            Callable[[TtyInteraction], Awaitable[str | None]] | None
        ) = None,
        renderer: TtyRenderer | None = None,
        console: Console | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._open_unix_connection = open_unix_connection
        self._read_message = read_message
        self._request_id_factory = request_id_factory
        self._prompt_session = prompt_session
        self._interaction_selector = interaction_selector
        self._renderer = renderer
        self._console = console
        self._pending_interactions: deque[TtyInteraction] = deque()

    async def run(
        self,
        *,
        input_fn: Callable[[str], str] | None = None,
    ) -> None:
        renderer = self._renderer
        if renderer is None:
            renderer = TtyRenderer(OutputBoundary(self._console or Console()))
        prompt_session = self._prompt_session
        owns_prompt_session = input_fn is None and prompt_session is None
        if input_fn is None and prompt_session is None:
            from prompt_toolkit import PromptSession

            prompt_session = PromptSession()
        interaction_selector = self._interaction_selector
        if interaction_selector is None and owns_prompt_session:
            interaction_selector = self._select_interaction_navigation

        try:
            reader, writer = await self._open_unix_connection(str(self._socket_path))
        except (OSError, ConnectionError) as exc:
            await renderer.error(
                "connection",
                f"cannot connect to {self._socket_path}: {exc}",
            )
            return

        session = SocketSession(
            reader,
            writer,
            read_message=self._read_message,
            request_id_factory=self._request_id_factory,
        )
        await renderer.connection_ready(self._socket_path)
        await renderer.help_hint()
        notification_task = asyncio.create_task(
            self._render_notifications(session, renderer),
            name="pal-tty-task-notifications",
        )

        async def read_input(prompt: str) -> str:
            if input_fn is not None:
                return input_fn(prompt)
            return await prompt_session.prompt_async(prompt)

        try:
            while True:
                try:
                    text = await read_input("pal> ")
                except EOFError:
                    break
                except KeyboardInterrupt:
                    await renderer.newline()
                    continue

                text = text.strip()
                if not text:
                    continue
                if text in {"/exit", "/quit"}:
                    break
                pending_result = await self._handle_pending_interaction(
                    session,
                    renderer,
                    text,
                    read_input=read_input,
                    interaction_selector=interaction_selector,
                )
                if pending_result is not None:
                    if pending_result:
                        break
                    continue
                if await self._handle_turn(
                    session,
                    renderer,
                    text,
                    read_input=read_input,
                    interaction_selector=interaction_selector,
                ):
                    break
        finally:
            notification_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await notification_task
            await session.aclose()

    async def _render_notifications(
        self,
        session: SocketSession,
        renderer: TtyRenderer,
    ) -> None:
        text_by_request: dict[str, list[str]] = {}
        try:
            async for payload in session.stream_notifications():
                request_id = str(payload.get("request_id") or "")
                payload_type = str(payload.get("type") or "")
                interaction = TtyInteraction.from_payload(payload)
                if interaction is not None:
                    if interaction.state in {"interactive_open", "interactive_update"}:
                        self._remember_pending_interaction(interaction)
                    elif interaction.state in {"interactive_resolve", "interactive_expire"}:
                        self._forget_pending_interaction(interaction.interaction_id)
                    await renderer.interaction(interaction)
                    continue
                if payload_type == "text_delta":
                    text_by_request.setdefault(request_id, []).append(
                        str(payload.get("text") or "")
                    )
                    continue
                if payload_type in {"done", "llm_done"}:
                    text = "".join(text_by_request.pop(request_id, []))
                    if text.strip():
                        await renderer.notification(text)
                    continue
                if payload_type in {"error", "llm_error"}:
                    await renderer.error(
                        "protocol",
                        str(payload.get("error_text") or "Task notification failed."),
                    )
        except (SocketDisconnected, SocketProtocolError):
            return

    def _remember_pending_interaction(self, interaction: TtyInteraction) -> None:
        self._forget_pending_interaction(interaction.interaction_id)
        self._pending_interactions.append(interaction)

    def _forget_pending_interaction(self, interaction_id: str) -> None:
        normalized = str(interaction_id or "").strip()
        if not normalized:
            return
        self._pending_interactions = deque(
            item
            for item in self._pending_interactions
            if item.interaction_id != normalized
        )

    async def _handle_pending_interaction(
        self,
        session: SocketSession,
        renderer: TtyRenderer,
        text: str,
        *,
        read_input: Callable[[str], Awaitable[str]],
        interaction_selector: (
            Callable[[TtyInteraction], Awaitable[str | None]] | None
        ),
    ) -> bool | None:
        """Apply a simple FIFO background interaction without a second prompt.

        A numeric choice or ``/cancel`` is consumed by the oldest pending
        interaction.  Any other text remains an ordinary user message.
        """
        if not self._pending_interactions:
            return None
        interaction = self._pending_interactions[0]
        if text.lower() == "/cancel":
            self._pending_interactions.popleft()
            return False
        try:
            selected_index = int(text)
        except ValueError:
            return None
        if selected_index < 1 or selected_index > len(interaction.options):
            return None
        self._pending_interactions.popleft()
        request_id = await session.send_interaction_result(
            interaction_id=interaction.interaction_id,
            button_token=interaction.options[selected_index - 1].token,
        )
        return await self._handle_response_loop(
            session,
            renderer,
            request_id,
            render_as_markdown=True,
            read_input=read_input,
            interaction_selector=interaction_selector,
        )

    async def _handle_turn(
        self,
        session: SocketSession,
        renderer: TtyRenderer,
        text: str,
        *,
        read_input: Callable[[str], Awaitable[str]],
        interaction_selector: (
            Callable[[TtyInteraction], Awaitable[str | None]] | None
        ),
    ) -> bool:
        try:
            request_id = await session.send(text)
            return await self._handle_response_loop(
                session,
                renderer,
                request_id,
                render_as_markdown=not text.startswith("/"),
                read_input=read_input,
                interaction_selector=interaction_selector,
            )
        except (SocketDisconnected, SocketProtocolError) as exc:
            await renderer.error("connection", str(exc))
            return True
        except KeyboardInterrupt:
            await renderer.error("connection", "interrupted")
            return False

    async def _handle_response_loop(
        self,
        session: SocketSession,
        renderer: TtyRenderer,
        request_id: str,
        *,
        render_as_markdown: bool,
        read_input: Callable[[str], Awaitable[str]],
        interaction_selector: (
            Callable[[TtyInteraction], Awaitable[str | None]] | None
        ),
    ) -> bool:
        try:
            while True:
                body = renderer.new_respondent(
                    render_as_markdown=render_as_markdown,
                )
                body.begin()
                interaction: TtyInteraction | None = None
                try:
                    async for payload in session.stream_response(request_id):
                        projected = await self._dispatch(payload, renderer, body)
                        if projected is not None:
                            interaction = projected
                finally:
                    await renderer.thinking_close()
                    body.finalize()
                if interaction is None:
                    return False
                if not interaction.options:
                    await renderer.interaction(interaction)
                    return False
                if interaction_selector is not None:
                    selected = await interaction_selector(interaction)
                else:
                    await renderer.interaction(interaction)
                    selected = await self._select_interaction(
                        interaction,
                        renderer=renderer,
                        read_input=read_input,
                    )
                if selected is None:
                    return True
                if not selected:
                    return False
                request_id = await session.send_interaction_result(
                    interaction_id=interaction.interaction_id,
                    button_token=selected,
                )
                render_as_markdown = True
        except (SocketDisconnected, SocketProtocolError) as exc:
            await renderer.error("connection", str(exc))
            return True
        except KeyboardInterrupt:
            await renderer.error("connection", "interrupted")
            return False

    async def _select_interaction(
        self,
        interaction: TtyInteraction,
        *,
        renderer: TtyRenderer,
        read_input: Callable[[str], Awaitable[str]],
    ) -> str | None:
        while True:
            try:
                raw = (
                    await read_input(
                        f"select [1-{len(interaction.options)}, 0 cancel]> "
                    )
                ).strip()
            except EOFError:
                return None
            except KeyboardInterrupt:
                await renderer.newline()
                return ""
            if raw.lower() in {"0", "/cancel"}:
                return ""
            try:
                selected_index = int(raw)
            except ValueError:
                selected_index = 0
            if 1 <= selected_index <= len(interaction.options):
                return interaction.options[selected_index - 1].token
            await renderer.invalid_selection(len(interaction.options))

    async def _select_interaction_navigation(
        self,
        interaction: TtyInteraction,
    ) -> str | None:
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.shortcuts.choice_input import ChoiceInput
        from prompt_toolkit.styles import Style

        bindings = KeyBindings()

        @bindings.add("escape", eager=True)
        @bindings.add("0", eager=True)
        def cancel(event) -> None:
            event.app.exit(result="", style="class:aborting")

        @bindings.add("c-d", eager=True)
        def exit_tty(event) -> None:
            event.app.exit(result=None, style="class:aborting")

        for index, option in enumerate(interaction.options[:9], start=1):
            self._bind_direct_choice(
                bindings,
                key=str(index),
                token=option.token,
            )

        selector = ChoiceInput[str | None](
            message=interaction.text or "Choose an action:",
            options=[
                (option.token, option.label)
                for option in interaction.options
            ],
            default=interaction.options[0].token,
            symbol="›",
            bottom_toolbar=(
                "↑/↓ move  •  Enter select  •  1–9 quick select  "
                "•  Esc/0 cancel"
            ),
            style=Style.from_dict(
                {
                    "selected-option": "bold reverse",
                    "bottom-toolbar": "bg:#333333 #dddddd",
                }
            ),
            key_bindings=bindings,
        )
        try:
            # ChoiceInput does not expose Application.erase_when_done even
            # though a completed slash-command menu is transient UI. Build
            # its application once, mark it transient, and run that exact
            # application so the selected menu is removed from the terminal.
            application = selector._create_application()
            application.erase_when_done = True
            return await application.run_async()
        except KeyboardInterrupt:
            return ""

    @staticmethod
    def _bind_direct_choice(
        bindings: Any,
        *,
        key: str,
        token: str,
    ) -> None:
        @bindings.add(key, eager=True)
        def select(event) -> None:
            event.app.exit(result=token, style="class:accepted")

    async def _dispatch(
        self,
        payload: dict[str, Any],
        renderer: TtyRenderer,
        body: AssistantBodyRenderer,
    ) -> TtyInteraction | None:
        payload_type = str(payload.get("type") or "")
        interaction = TtyInteraction.from_payload(payload)
        if interaction is not None:
            return interaction
        if payload_type == "reasoning_delta":
            chunk = str(payload.get("reasoning_text") or payload.get("text") or "")
            if chunk:
                await renderer.thinking_delta(chunk)
            return None
        if payload_type == "text_delta":
            chunk = str(payload.get("text") or "")
            if chunk:
                await renderer.thinking_close()
                body.feed(chunk)
            return None
        if payload_type == "op_tool_call":
            tool_call = dict(payload.get("op_tool_call") or {})
            await renderer.tool_call(str(tool_call.get("name") or "tool"))
            return None
        if payload_type == "llm_error":
            chunk = str(payload.get("error_text") or payload.get("text") or "unknown error")
            if chunk:
                await renderer.error("llm", chunk)
            return None
        if payload_type == "error":
            chunk = str(payload.get("error_text") or payload.get("text") or "unknown error")
            if chunk:
                await renderer.error("protocol", chunk)
        return None

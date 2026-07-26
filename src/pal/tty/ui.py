"""Async interactive loop for the Pal socket TTY."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from rich.console import Console

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
        renderer: TtyRenderer | None = None,
        console: Console | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._open_unix_connection = open_unix_connection
        self._read_message = read_message
        self._request_id_factory = request_id_factory
        self._prompt_session = prompt_session
        self._renderer = renderer
        self._console = console

    async def run(
        self,
        *,
        input_fn: Callable[[str], str] | None = None,
    ) -> None:
        renderer = self._renderer
        if renderer is None:
            renderer = TtyRenderer(OutputBoundary(self._console or Console()))
        prompt_session = self._prompt_session
        if input_fn is None and prompt_session is None:
            from prompt_toolkit import PromptSession

            prompt_session = PromptSession()

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
        try:
            while True:
                try:
                    if input_fn is not None:
                        text = input_fn("pal> ")
                    else:
                        text = await prompt_session.prompt_async("pal> ")
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
                if await self._handle_turn(session, renderer, text):
                    break
        finally:
            await session.aclose()

    async def _handle_turn(
        self,
        session: SocketSession,
        renderer: TtyRenderer,
        text: str,
    ) -> bool:
        body = renderer.new_respondent(
            render_as_markdown=not text.startswith("/"),
        )
        body.begin()
        try:
            request_id = await session.send(text)
            async for payload in session.stream_response(request_id):
                await self._dispatch(payload, renderer, body)
            await renderer.thinking_close()
            body.finalize()
            return False
        except (SocketDisconnected, SocketProtocolError) as exc:
            await renderer.thinking_close()
            body.finalize()
            await renderer.error("connection", str(exc))
            return True
        except KeyboardInterrupt:
            await renderer.thinking_close()
            body.finalize()
            await renderer.error("connection", "interrupted")
            return False

    async def _dispatch(
        self,
        payload: dict[str, Any],
        renderer: TtyRenderer,
        body: AssistantBodyRenderer,
    ) -> None:
        payload_type = str(payload.get("type") or "")
        if payload_type == "reasoning_delta":
            chunk = str(payload.get("reasoning_text") or payload.get("text") or "")
            if chunk:
                await renderer.thinking_delta(chunk)
            return
        if payload_type == "text_delta":
            chunk = str(payload.get("text") or "")
            if chunk:
                await renderer.thinking_close()
                body.feed(chunk)
            return
        if payload_type == "op_tool_call":
            tool_call = dict(payload.get("op_tool_call") or {})
            await renderer.tool_call(str(tool_call.get("name") or "tool"))
            return
        if payload_type == "llm_error":
            chunk = str(payload.get("error_text") or payload.get("text") or "unknown error")
            if chunk:
                await renderer.error("llm", chunk)
            return
        if payload_type == "error":
            chunk = str(payload.get("error_text") or payload.get("text") or "unknown error")
            if chunk:
                await renderer.error("protocol", chunk)

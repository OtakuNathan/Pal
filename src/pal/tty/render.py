"""Rich rendering that cooperates with Prompt Toolkit terminal ownership."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from prompt_toolkit.application import get_app_or_none, run_in_terminal
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text


class OutputBoundary:
    """The sole path for Rich writes made by the interactive TTY."""

    def __init__(self, console: Console) -> None:
        self._console = console

    @property
    def console(self) -> Console:
        return self._console

    async def render(self, renderable: Any, **print_kwargs: Any) -> None:
        app = get_app_or_none()
        if app is not None:
            await run_in_terminal(
                lambda: self._console.print(renderable, **print_kwargs)
            )
            return
        self._console.print(renderable, **print_kwargs)


class AssistantBodyRenderer:
    """Render one streamed assistant body exactly once in its terminal region."""

    def __init__(
        self,
        console: Console,
        *,
        live_factory: Callable[..., Live] = Live,
        markdown_factory: Callable[[str], Markdown] = Markdown,
        render_as_markdown: bool = True,
        refresh_per_second: float = 8.0,
    ) -> None:
        self._console = console
        self._live_factory = live_factory
        self._markdown_factory = markdown_factory
        self._render_as_markdown = render_as_markdown
        self._refresh_per_second = refresh_per_second
        self._buffer: list[str] = []
        self._live: Live | None = None

    def begin(self) -> None:
        self._buffer = []
        self._live = None

    def feed(self, text: str) -> None:
        if not text:
            return
        self._buffer.append(text)
        body = "".join(self._buffer)
        if self._console.is_terminal and self._live is None:
            self._live = self._live_factory(
                self._renderable(body),
                console=self._console,
                refresh_per_second=self._refresh_per_second,
            )
            self._live.start()
        elif self._live is not None:
            self._live.update(self._renderable(body))

    def finalize(self) -> None:
        body = "".join(self._buffer)
        live = self._live
        self._live = None
        if live is not None:
            try:
                if body:
                    live.update(self._renderable(body))
            finally:
                try:
                    live.stop()
                except Exception:
                    pass
            return
        if body.strip():
            self._console.print(self._renderable(body))

    def _renderable(self, body: str) -> Markdown | Text:
        if self._render_as_markdown:
            return self._markdown_factory(body)
        return Text(body)


class TtyRenderer:
    """Render reasoning, tools, errors and assistant Markdown distinctly."""

    def __init__(
        self,
        output: OutputBoundary,
        *,
        live_factory: Callable[..., Live] = Live,
        markdown_factory: Callable[[str], Markdown] = Markdown,
    ) -> None:
        self._output = output
        self._live_factory = live_factory
        self._markdown_factory = markdown_factory
        self._reasoning_open = False

    def new_respondent(
        self,
        *,
        render_as_markdown: bool = True,
    ) -> AssistantBodyRenderer:
        return AssistantBodyRenderer(
            self._output.console,
            live_factory=self._live_factory,
            markdown_factory=self._markdown_factory,
            render_as_markdown=render_as_markdown,
        )

    async def connection_ready(self, socket_path: Path) -> None:
        await self._output.render(Text(f"connected to {socket_path}", style="dim"))

    async def help_hint(self) -> None:
        await self._output.render(Text("type /exit or Ctrl-D to quit", style="dim"))

    async def thinking_delta(self, text: str) -> None:
        if not self._reasoning_open:
            self._reasoning_open = True
            await self._output.render(Text("[thinking]", style="dim italic"))
        await self._output.render(
            Text(text, style="dim"),
            end="",
            soft_wrap=True,
            highlight=False,
        )

    async def thinking_close(self) -> None:
        if self._reasoning_open:
            self._reasoning_open = False
            await self._output.render("")

    async def tool_call(self, name: str) -> None:
        await self.thinking_close()
        await self._output.render(Text(f"[tool] {name}", style="cyan"))

    async def error(self, kind: str, text: str) -> None:
        await self.thinking_close()
        prefix = {
            "llm": "[llm-error]",
            "protocol": "[error]",
            "connection": "[error]",
        }.get(kind, "[error]")
        await self._output.render(Text(f"{prefix} {text}", style="red"))

    async def newline(self) -> None:
        await self._output.render("")

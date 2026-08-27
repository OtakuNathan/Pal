from __future__ import annotations

import asyncio
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from prompt_toolkit.shortcuts.choice_input import ChoiceInput
from rich.console import Console
from rich.markdown import Markdown

from pal.tty.render import AssistantBodyRenderer, OutputBoundary, TtyRenderer
from pal.tty.interactions import TtyInteraction, TtyInteractionOption
from pal.tty.session import SocketDisconnected, SocketProtocolError, SocketSession
from pal.tty.ui import TtyRepl


_EOF = object()
_INTERRUPT = object()


class _FakeWriter:
    def __init__(self, *, drain_error: BaseException | None = None) -> None:
        self.written = b""
        self.closed = False
        self.drain_error = drain_error

    def write(self, data: bytes) -> None:
        self.written += bytes(data)

    async def drain(self) -> None:
        if self.drain_error is not None:
            raise self.drain_error
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FakePrompt:
    def __init__(self, inputs: list[object]) -> None:
        self._inputs = iter(inputs)

    async def prompt_async(self, _message: str = "") -> str:
        item = next(self._inputs, _EOF)
        if item is _EOF:
            raise EOFError
        if item is _INTERRUPT:
            raise KeyboardInterrupt
        return str(item)


class _FakeLive:
    instances: list[_FakeLive] = []

    def __init__(self, renderable=None, **_kwargs: object) -> None:
        self.renderable = renderable
        self.started = False
        self.stopped = False
        self.updates: list[object] = []
        self.instances.append(self)

    def start(self) -> None:
        self.started = True

    def update(self, renderable: object) -> None:
        self.updates.append(renderable)

    def stop(self) -> None:
        self.stopped = True


def _console(*, terminal: bool = False) -> Console:
    return Console(
        file=io.StringIO(),
        force_terminal=terminal,
        width=80,
        highlight=False,
    )


def _build_repl(
    events: list[dict],
    inputs: list[object],
    *,
    ids: list[str],
    open_error: BaseException | None = None,
    read_error: BaseException | None = None,
    interaction_selector=None,
) -> tuple[TtyRepl, _FakeWriter, Console]:
    writer = _FakeWriter()
    event_iterator = iter(events)
    id_iterator = iter(ids)

    async def open_connection(_path: str):
        if open_error is not None:
            raise open_error
        return object(), writer

    async def read_message(_reader):
        if read_error is not None:
            raise read_error
        return next(event_iterator)

    console = _console()
    repl = TtyRepl(
        Path("/tmp/pal.sock"),
        open_unix_connection=open_connection,
        read_message=read_message,
        request_id_factory=lambda: next(id_iterator),
        prompt_session=_FakePrompt(inputs),
        interaction_selector=interaction_selector,
        renderer=TtyRenderer(OutputBoundary(console)),
    )
    return repl, writer, console


class SocketSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_unsolicited_task_notifications_do_not_consume_turn_response(self) -> None:
        events = iter(
            [
                {
                    "type": "text_delta",
                    "request_id": "task-notification:d1",
                    "text": "Task finished.",
                },
                {
                    "type": "done",
                    "request_id": "task-notification:d1",
                    "finish_reason": "stop",
                },
                {"type": "text_delta", "request_id": "r1", "text": "reply"},
                {"type": "done", "request_id": "r1", "finish_reason": "stop"},
            ]
        )

        async def read_message(_reader):
            return next(events)

        writer = _FakeWriter()
        session = SocketSession(object(), writer, read_message, lambda: "r1")

        async def collect_notification() -> list[dict[str, object]]:
            result = []
            async for payload in session.stream_notifications():
                result.append(payload)
                if payload.get("type") == "done":
                    return result
            return result

        notification_task = asyncio.create_task(collect_notification())
        response = [item async for item in session.stream_response("r1")]
        notification = await notification_task

        self.assertEqual(notification[0]["text"], "Task finished.")
        self.assertEqual(response[0]["text"], "reply")
        self.assertIn(b"session_ready", writer.written)

    async def test_demuxes_requests_and_tool_call_finish_is_not_terminal(self) -> None:
        events = iter(
            [
                {"type": "text_delta", "request_id": "other", "text": "skip"},
                {"type": "text_delta", "request_id": "r1", "text": "one"},
                {
                    "type": "llm_done",
                    "request_id": "r1",
                    "finish_reason": "tool_calls",
                },
                {"type": "text_delta", "request_id": "r1", "text": "two"},
                {"type": "done", "request_id": "r1", "finish_reason": "stop"},
            ]
        )

        async def read_message(_reader):
            return next(events)

        session = SocketSession(
            object(),
            _FakeWriter(),
            read_message,
            lambda: "r1",
        )
        seen = [item async for item in session.stream_response("r1")]

        self.assertEqual(
            [
                item["text"]
                for item in seen
                if item.get("type") == "text_delta"
            ],
            ["one", "two"],
        )
        self.assertEqual(seen[-1]["type"], "done")

    async def test_send_preserves_user_and_slash_wire_types(self) -> None:
        writer = _FakeWriter()
        session = SocketSession(
            object(),
            writer,
            lambda _reader: None,
            lambda: "request-id",
        )

        await session.send("hello")
        await session.send("/status")

        self.assertIn(b"user_message", writer.written)
        self.assertIn(b"slash_command", writer.written)
        self.assertIn(b"hello", writer.written)
        self.assertIn(b"/status", writer.written)
        self.assertIn(b"session_ready", writer.written)
        self.assertIn(b"delivery_ack_v1", writer.written)

    async def test_send_interaction_result_keeps_action_semantics_server_side(self) -> None:
        writer = _FakeWriter()
        session = SocketSession(
            object(),
            writer,
            lambda _reader: None,
            lambda: "request-id",
        )

        request_id = await session.send_interaction_result(
            interaction_id="choose-model",
            button_token="b1",
        )

        self.assertEqual(request_id, "request-id")
        self.assertIn(b"interaction_result", writer.written)
        self.assertIn(b"choose-model", writer.written)
        self.assertIn(b"b1", writer.written)
        self.assertNotIn(b"action_key", writer.written)
        self.assertNotIn(b"action_args", writer.written)

    async def test_send_maps_write_failure_to_disconnected(self) -> None:
        session = SocketSession(
            object(),
            _FakeWriter(drain_error=BrokenPipeError("closed")),
            lambda _reader: None,
            lambda: "request-id",
        )

        with self.assertRaisesRegex(SocketDisconnected, "socket write failed"):
            await session.send("hello")

    async def test_maps_eof_and_malformed_frames_to_typed_errors(self) -> None:
        async def disconnected(_reader):
            raise asyncio.IncompleteReadError(b"", 4)

        async def malformed(_reader):
            raise ValueError("bad frame")

        for reader, error_type in (
            (disconnected, SocketDisconnected),
            (malformed, SocketProtocolError),
        ):
            with self.subTest(error_type=error_type.__name__):
                session = SocketSession(
                    object(),
                    _FakeWriter(),
                    reader,
                    lambda: "r1",
                )
                with self.assertRaises(error_type):
                    async for _ in session.stream_response("r1"):
                        pass

    async def test_close_is_idempotent_for_writer_failures(self) -> None:
        writer = _FakeWriter()
        session = SocketSession(
            object(),
            writer,
            lambda _reader: None,
            lambda: "r1",
        )

        await session.aclose()

        self.assertTrue(writer.closed)


class TtyReplTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_interaction_is_erased_after_selection(self) -> None:
        repl, _writer, _console_value = _build_repl([], [], ids=[])

        class _Application:
            erase_when_done = False

            async def run_async(self):
                return "b1"

        application = _Application()
        interaction = TtyInteraction(
            interaction_id="choose-model",
            interaction_kind="model_select",
            state="interactive_open",
            text="Choose a model.",
            options=(
                TtyInteractionOption(label="Fast", token="b0"),
                TtyInteractionOption(label="Deep", token="b1"),
            ),
        )

        with patch.object(ChoiceInput, "_create_application", return_value=application):
            selected = await repl._select_interaction_navigation(interaction)

        self.assertEqual(selected, "b1")
        self.assertTrue(application.erase_when_done)

    async def test_multi_turn_reuses_socket_and_renders_markdown_once(self) -> None:
        repl, writer, console = _build_repl(
            [
                {"type": "text_delta", "request_id": "r1", "text": "# Hi\n"},
                {
                    "type": "text_delta",
                    "request_id": "r1",
                    "text": "body **bold**",
                },
                {
                    "type": "llm_done",
                    "request_id": "r1",
                    "finish_reason": "stop",
                },
                {
                    "type": "text_delta",
                    "request_id": "r2",
                    "text": "status one\nstatus two",
                },
                {"type": "done", "request_id": "r2", "finish_reason": "stop"},
            ],
            ["hello", "/status", "/exit"],
            ids=["r1", "r2"],
        )

        await repl.run()

        rendered = console.file.getvalue()
        self.assertIn(b"user_message", writer.written)
        self.assertIn(b"slash_command", writer.written)
        self.assertIn(b"r1", writer.written)
        self.assertIn(b"r2", writer.written)
        self.assertIn("Hi", rendered)
        self.assertIn("bold", rendered)
        self.assertEqual(rendered.count("status one"), 1)
        self.assertIn("status one\nstatus two", rendered)
        self.assertTrue(writer.closed)

    async def test_empty_input_interrupt_eof_and_quit_are_local_controls(self) -> None:
        repl, writer, _console_value = _build_repl(
            [
                {"type": "text_delta", "request_id": "r1", "text": "ok"},
                {"type": "done", "request_id": "r1", "finish_reason": "stop"},
            ],
            ["", "  ", _INTERRUPT, "hello", _EOF],
            ids=["r1"],
        )

        await repl.run()

        self.assertEqual(writer.written.count(b"user_message"), 1)
        self.assertIn(b"hello", writer.written)
        self.assertTrue(writer.closed)

    async def test_sync_input_seam_preserves_eof_and_interrupt_behavior(self) -> None:
        writer = _FakeWriter()
        events = iter(
            [
                {"type": "text_delta", "request_id": "r1", "text": "ok"},
                {"type": "done", "request_id": "r1", "finish_reason": "stop"},
            ]
        )
        inputs = iter([_INTERRUPT, "hello", _EOF])

        async def open_connection(_path: str):
            return object(), writer

        async def read_message(_reader):
            return next(events)

        def input_fn(_prompt: str) -> str:
            item = next(inputs)
            if item is _EOF:
                raise EOFError
            if item is _INTERRUPT:
                raise KeyboardInterrupt
            return str(item)

        repl = TtyRepl(
            Path("/tmp/pal.sock"),
            open_unix_connection=open_connection,
            read_message=read_message,
            request_id_factory=lambda: "r1",
            renderer=TtyRenderer(OutputBoundary(_console())),
        )

        await repl.run(input_fn=input_fn)

        self.assertIn(b"hello", writer.written)
        self.assertTrue(writer.closed)

    async def test_reasoning_tools_and_errors_have_separate_rendering(self) -> None:
        repl, writer, console = _build_repl(
            [
                {
                    "type": "reasoning_delta",
                    "request_id": "r1",
                    "reasoning_text": "thinking",
                },
                {
                    "type": "op_tool_call",
                    "request_id": "r1",
                    "op_tool_call": {"name": "read_file", "args": {}},
                },
                {
                    "type": "llm_error",
                    "request_id": "r1",
                    "error_text": "retrying",
                },
                {
                    "type": "text_delta",
                    "request_id": "r1",
                    "text": "final",
                },
                {"type": "done", "request_id": "r1", "finish_reason": "stop"},
            ],
            ["hello", "/exit"],
            ids=["r1"],
        )

        await repl.run()

        rendered = console.file.getvalue()
        self.assertIn("[thinking]", rendered)
        self.assertIn("thinking", rendered)
        self.assertIn("[tool] read_file", rendered)
        self.assertIn("[llm-error] retrying", rendered)
        self.assertIn("final", rendered)
        self.assertTrue(writer.closed)

    async def test_inline_interaction_renders_numbered_choices_and_submits_token(self) -> None:
        repl, writer, console = _build_repl(
            [
                {
                    "type": "interactive_open",
                    "request_id": "r1",
                    "interaction": {
                        "interaction_id": "choose-model",
                        "interaction_kind": "model_select",
                        "text": "Choose the active model.",
                        "buttons": [
                            [{"label": "Fast", "token": "b0"}],
                            [{"label": "Deep", "token": "b1"}],
                        ],
                        "expires_at": None,
                    },
                },
                {"type": "done", "request_id": "r1", "finish_reason": "stop"},
                {
                    "type": "interactive_resolve",
                    "request_id": "r2",
                    "interaction": {
                        "interaction_id": "choose-model",
                        "interaction_kind": "model_select",
                        "text": "Active model: Deep",
                        "buttons": [],
                        "expires_at": None,
                    },
                },
                {"type": "done", "request_id": "r2", "finish_reason": "stop"},
            ],
            ["/model", "invalid", "2", "/exit"],
            ids=["r1", "r2"],
        )

        await repl.run()

        rendered = console.file.getvalue()
        self.assertIn("Choose the active model.", rendered)
        self.assertIn("1. Fast", rendered)
        self.assertIn("2. Deep", rendered)
        self.assertIn("0. Cancel", rendered)
        self.assertIn("Choose a number from 1 to 2, or 0 to cancel.", rendered)
        self.assertIn("Active model: Deep", rendered)
        self.assertIn(b"interaction_result", writer.written)
        self.assertIn(b"choose-model", writer.written)
        self.assertIn(b"b1", writer.written)
        self.assertNotIn(b"action_key", writer.written)
        self.assertTrue(writer.closed)

    async def test_inline_interaction_can_be_locally_cancelled_without_action(self) -> None:
        repl, writer, _console_value = _build_repl(
            [
                {
                    "type": "interactive_open",
                    "request_id": "r1",
                    "interaction": {
                        "interaction_id": "control-panel",
                        "interaction_kind": "control_panel",
                        "text": "Choose an action.",
                        "buttons": [[{"label": "Status", "token": "b0"}]],
                    },
                },
                {"type": "done", "request_id": "r1", "finish_reason": "stop"},
                {"type": "text_delta", "request_id": "r2", "text": "hello"},
                {"type": "done", "request_id": "r2", "finish_reason": "stop"},
            ],
            ["/control", "0", "hello", "/exit"],
            ids=["r1", "r2"],
        )

        await repl.run()

        self.assertNotIn(b"interaction_result", writer.written)
        self.assertIn(b"hello", writer.written)
        self.assertTrue(writer.closed)

    async def test_injected_navigation_selector_submits_selected_token(self) -> None:
        seen_interactions = []

        async def interaction_selector(interaction):
            seen_interactions.append(interaction)
            return "b1"

        repl, writer, _console_value = _build_repl(
            [
                {
                    "type": "interactive_open",
                    "request_id": "r1",
                    "interaction": {
                        "interaction_id": "choose-model",
                        "interaction_kind": "model_select",
                        "text": "Choose a model.",
                        "buttons": [[
                            {"label": "Fast", "token": "b0"},
                            {"label": "Deep", "token": "b1"},
                        ]],
                    },
                },
                {"type": "done", "request_id": "r1", "finish_reason": "stop"},
                {
                    "type": "interactive_resolve",
                    "request_id": "r2",
                    "interaction": {
                        "interaction_id": "choose-model",
                        "interaction_kind": "model_select",
                        "text": "Active model: Deep",
                        "buttons": [],
                    },
                },
                {"type": "done", "request_id": "r2", "finish_reason": "stop"},
            ],
            ["/model", "/exit"],
            ids=["r1", "r2"],
            interaction_selector=interaction_selector,
        )

        await repl.run()

        self.assertEqual(
            [interaction.interaction_id for interaction in seen_interactions],
            ["choose-model"],
        )
        self.assertIn(b"interaction_result", writer.written)
        self.assertIn(b"b1", writer.written)
        self.assertTrue(writer.closed)

    async def test_connect_failure_is_rendered_without_open_writer(self) -> None:
        repl, writer, console = _build_repl(
            [],
            [],
            ids=[],
            open_error=ConnectionRefusedError("not running"),
        )

        await repl.run()

        self.assertIn("cannot connect", console.file.getvalue())
        self.assertIn("not running", console.file.getvalue())
        self.assertFalse(writer.closed)

    async def test_disconnect_is_rendered_and_closes_writer(self) -> None:
        repl, writer, console = _build_repl(
            [],
            ["hello"],
            ids=["r1"],
            read_error=asyncio.IncompleteReadError(b"", 4),
        )

        await repl.run()

        self.assertIn("socket closed by peer", console.file.getvalue())
        self.assertTrue(writer.closed)


class TtyReasoningRendererTests(unittest.IsolatedAsyncioTestCase):
    async def test_non_terminal_reasoning_is_one_status_not_raw_token_spam(self) -> None:
        console = _console(terminal=False)
        renderer = TtyRenderer(OutputBoundary(console))

        await renderer.thinking_delta("!!!!,,,,....")
        await renderer.thinking_delta("repeated private reasoning")
        await renderer.thinking_delta("repeated private reasoning")
        await renderer.thinking_close()

        rendered = console.file.getvalue()
        self.assertEqual(rendered.count("[thinking]"), 1)
        self.assertNotIn("!!!!", rendered)
        self.assertNotIn("private reasoning", rendered)

    async def test_terminal_reasoning_uses_one_bounded_transient_live_region(self) -> None:
        console = _console(terminal=True)
        _FakeLive.instances.clear()
        renderer = TtyRenderer(
            OutputBoundary(console),
            live_factory=_FakeLive,
        )

        await renderer.thinking_delta("......")
        await renderer.thinking_delta("inspect the active contract")
        await renderer.thinking_delta("inspect the active contract")
        await renderer.thinking_delta("x" * 600)
        await renderer.thinking_close()

        self.assertEqual(len(_FakeLive.instances), 1)
        live = _FakeLive.instances[0]
        self.assertTrue(live.started)
        self.assertTrue(live.stopped)
        self.assertEqual(len(live.updates), 2)
        preview = live.updates[-1]
        self.assertLessEqual(len(preview.plain), 252)
        self.assertTrue(preview.plain.startswith("[thinking] …"))
        self.assertEqual(console.file.getvalue(), "")


class AssistantBodyRendererTests(unittest.TestCase):
    def test_non_terminal_renders_one_final_markdown_body(self) -> None:
        console = _console(terminal=False)
        _FakeLive.instances.clear()
        body = AssistantBodyRenderer(console, live_factory=_FakeLive)

        body.begin()
        body.feed("# Title")
        body.feed("\n- item")
        body.finalize()

        self.assertEqual(_FakeLive.instances, [])
        self.assertIn("Title", console.file.getvalue())
        self.assertIn("item", console.file.getvalue())

    def test_terminal_freezes_live_region_without_second_print(self) -> None:
        console = _console(terminal=True)
        _FakeLive.instances.clear()
        body = AssistantBodyRenderer(console, live_factory=_FakeLive)

        body.begin()
        body.feed("# Heading")
        body.feed(" more")
        body.finalize()

        self.assertEqual(len(_FakeLive.instances), 1)
        live = _FakeLive.instances[0]
        self.assertTrue(live.started)
        self.assertTrue(live.stopped)
        self.assertTrue(
            any(isinstance(renderable, Markdown) for renderable in live.updates)
        )
        self.assertEqual(console.file.getvalue(), "")

    def test_empty_body_renders_nothing(self) -> None:
        console = _console()
        body = AssistantBodyRenderer(console)

        body.begin()
        body.finalize()

        self.assertEqual(console.file.getvalue(), "")


class OutputBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_render_without_active_prompt_application(self) -> None:
        console = _console()

        await OutputBoundary(console).render("hello")

        self.assertIn("hello", console.file.getvalue())

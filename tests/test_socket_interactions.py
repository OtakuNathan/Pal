from __future__ import annotations

import unittest

from pal.channel.contracts import ChannelStreamUpdate, EndpointConfig, ResponseHandle
from pal.channel.endpoints.socket_endpoint import SocketChannelEndpoint
from pal.channel.runtime import ChannelRuntime
from pal.control.contracts import InteractionButtonSpec, InteractionMessageSpec
from pal.shared import ChannelStreamUpdateKind, EventKind


class _OutboundQueue:
    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def put_nowait(self, item: dict[str, object]) -> None:
        self.items.append(item)


class _Session:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.request_ids: set[str] = set()
        self.outbound = _OutboundQueue()
        self.closed = False


class SocketInteractionProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = SocketChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="socket_default",
                channel_kind="socket",
                binding_key="pal.sock",
            )
        )
        self.session = _Session("session-1")
        self.endpoint.sessions[self.session.session_id] = self.session
        self.response_handle = ResponseHandle(
            endpoint_id="socket_default",
            reply_target={
                "session_id": self.session.session_id,
                "request_id": "request-1",
            },
        )
        self.spec = InteractionMessageSpec(
            interaction_id="choose-model",
            interaction_kind="model_select",
            text="Choose a model.",
            buttons=(
                (
                    InteractionButtonSpec(
                        label="Fast",
                        action_key="select_model",
                        action_args={"model_id": "fast"},
                    ),
                    InteractionButtonSpec(
                        label="Deep",
                        action_key="select_model",
                        action_args={"model_id": "deep"},
                    ),
                ),
            ),
        )

    def test_open_projects_labels_and_opaque_tokens_then_terminates_response(self) -> None:
        self.endpoint.send_status(
            self.response_handle,
            "interactive_open",
            {"spec": self.spec},
        )

        self.assertEqual(
            [item["type"] for item in self.session.outbound.items],
            ["interactive_open", "done"],
        )
        interaction = self.session.outbound.items[0]["interaction"]
        self.assertEqual(interaction["interaction_id"], "choose-model")
        self.assertEqual(
            interaction["buttons"],
            [[
                {"label": "Fast", "token": "b0"},
                {"label": "Deep", "token": "b1"},
            ]],
        )
        self.assertNotIn("action_key", str(interaction))
        self.assertNotIn("model_id", str(interaction))

    def test_nonterminal_reply_keeps_socket_request_open(self) -> None:
        response_handle = ResponseHandle(
            endpoint_id="socket_default",
            reply_target={
                "session_id": self.session.session_id,
                "request_id": "request-1",
                "_pal_turn_continues": True,
            },
        )

        self.endpoint.send_reply(response_handle, "Checklist progress 1/3")

        self.assertEqual(
            self.session.outbound.items,
            [
                {
                    "type": "text_delta",
                    "request_id": "request-1",
                    "text": "Checklist progress 1/3",
                }
            ],
        )

    def test_terminal_stream_frame_carries_full_text_for_projection_repair(self) -> None:
        self.endpoint.send_stream_update(
            self.response_handle,
            ChannelStreamUpdate(
                kind=ChannelStreamUpdateKind.DONE,
                text="complete streamed reply",
                finish_reason="stop",
            ),
        )

        terminal = self.session.outbound.items[-1]
        self.assertEqual(terminal["type"], "llm_done")
        self.assertEqual(terminal["final_text"], "complete streamed reply")
        self.assertNotIn("text", terminal)

    def test_selected_token_restores_server_owned_action(self) -> None:
        self.endpoint.send_status(
            self.response_handle,
            "interactive_open",
            {"spec": self.spec},
        )

        self.endpoint._accept_interaction_result(
            self.session,
            {
                "type": "interaction_result",
                "request_id": "request-2",
                "interaction_id": "choose-model",
                "button_token": "b1",
            },
        )

        envelopes = self.endpoint.poll()
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(envelopes[0].event.event_kind, EventKind.INTERACTION_RESULT)
        result = envelopes[0].event.payload
        self.assertEqual(result.action_key, "select_model")
        self.assertEqual(result.action_args, {"model_id": "deep"})
        self.assertEqual(
            envelopes[0].response_handle.reply_target["request_id"],
            "request-2",
        )

    def test_pending_update_consumes_buttons_before_terminal_result(self) -> None:
        self.endpoint.send_status(
            self.response_handle,
            "interactive_open",
            {"spec": self.spec},
        )
        self.session.outbound.items.clear()
        pending = InteractionMessageSpec(
            interaction_id=self.spec.interaction_id,
            interaction_kind=self.spec.interaction_kind,
            text="Compacting…",
            buttons=(),
        )

        self.endpoint.send_status(
            self.response_handle,
            "interactive_update",
            {"spec": pending},
        )

        interaction = self.session.outbound.items[0]["interaction"]
        self.assertEqual(self.session.outbound.items[0]["type"], "interactive_update")
        self.assertEqual(interaction["buttons"], [])
        self.assertEqual(
            self.endpoint._interactive_messages[self.spec.interaction_id]["actions"],
            {},
        )

    def test_endpoint_status_can_be_flushed_before_long_control_action_finishes(self) -> None:
        runtime = ChannelRuntime()
        runtime.register_endpoint(self.endpoint)
        self.endpoint.queue_status(
            "interactive_update",
            response_handle=self.response_handle,
            payload={
                "spec": InteractionMessageSpec(
                    interaction_id=self.spec.interaction_id,
                    interaction_kind=self.spec.interaction_kind,
                    text="Compacting…",
                    buttons=(),
                )
            },
        )

        self.assertEqual(self.session.outbound.items, [])
        self.assertTrue(runtime.flush_endpoint_status("socket_default"))
        self.assertEqual(
            self.session.outbound.items[0]["type"],
            "interactive_update",
        )

    def test_other_socket_session_cannot_answer_interaction(self) -> None:
        self.endpoint.send_status(
            self.response_handle,
            "interactive_open",
            {"spec": self.spec},
        )
        other = _Session("session-2")

        self.endpoint._accept_interaction_result(
            other,
            {
                "type": "interaction_result",
                "request_id": "request-other",
                "interaction_id": "choose-model",
                "button_token": "b0",
            },
        )

        self.assertEqual(self.endpoint.poll(), [])
        self.assertEqual(other.outbound.items[0]["type"], "error")
        self.assertEqual(
            other.outbound.items[0]["request_id"],
            "request-other",
        )

    def test_rebound_replacement_session_can_answer_owned_interaction(self) -> None:
        self.endpoint.send_status(
            self.response_handle,
            "interactive_open",
            {"spec": self.spec},
        )
        replacement = _Session("session-2")
        self.endpoint._session_replacements[self.session.session_id] = replacement.session_id

        self.endpoint._accept_interaction_result(
            replacement,
            {
                "type": "interaction_result",
                "request_id": "request-rebound",
                "interaction_id": "choose-model",
                "button_token": "b0",
            },
        )

        envelopes = self.endpoint.poll()
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(envelopes[0].event.event_kind, EventKind.INTERACTION_RESULT)
        self.assertEqual(envelopes[0].event.payload.action_args, {"model_id": "fast"})
        self.assertEqual(
            envelopes[0].response_handle.reply_target["request_id"],
            "request-rebound",
        )


if __name__ == "__main__":
    unittest.main()

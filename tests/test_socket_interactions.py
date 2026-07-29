from __future__ import annotations

import unittest

from pal.channel.contracts import EndpointConfig, ResponseHandle
from pal.channel.endpoints.socket_endpoint import SocketChannelEndpoint
from pal.control.contracts import InteractionButtonSpec, InteractionMessageSpec
from pal.shared import EventKind


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


if __name__ == "__main__":
    unittest.main()

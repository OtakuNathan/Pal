from __future__ import annotations

import unittest
import json
from collections import UserDict
from copy import deepcopy
from dataclasses import asdict

from pal.artifact.contracts import ArtifactRef
from pal.artifact.prompt import _artifact_ids_from_payload
from pal.channel.ingress import ChannelIngressCompiler
from pal.foundation import EventEnvelope
from pal.llm.ir import (
    ArtifactRefPartIR,
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
    TextPartIR,
)
from pal.llm.serde import message_from_payload, message_to_payload
from pal.shared import ChannelEnvelope, EndpointConfig, EventKind, ResponseHandle, TurnDeliveryBinding


class _Artifacts:
    def register_ingested(self, _attachment, **_kwargs):
        return ArtifactRef(
            artifact_id="art-1",
            kind="image",
            file_name="shot.png",
            summary="screenshot",
            status="ready",
            available_actions=("info",),
        )


def _envelope(payload, *, endpoint_id: str = "a", event_kind: str = EventKind.USER_MESSAGE) -> ChannelEnvelope:
    return ChannelEnvelope(
        event=EventEnvelope(
            event_kind=event_kind,
            source_kind="channel",
            payload=payload,
            event_id="turn-1",
        ),
        endpoint=EndpointConfig(endpoint_id, "socket", endpoint_id),
        response_handle=ResponseHandle(endpoint_id, {"session_id": endpoint_id}),
    )


class ChannelIngressIRTests(unittest.TestCase):
    def test_caption_and_attachment_compile_to_one_l1_message(self) -> None:
        compiled = ChannelIngressCompiler(artifact_manager=_Artifacts()).compile(
            _envelope({"text": "look", "attachments": [{"path": "ignored"}]})
        )
        self.assertIsInstance(compiled.event.payload, LLMMessageIR)
        self.assertEqual(compiled.event.payload.text, "look")
        self.assertIsInstance(compiled.event.payload.parts[1], ArtifactRefPartIR)

    def test_attachment_only_and_idempotent_typed_payload(self) -> None:
        compiler = ChannelIngressCompiler(artifact_manager=_Artifacts())
        compiled = compiler.compile(_envelope({"attachments": [{}]}))
        self.assertEqual(len(compiled.event.payload.parts), 1)
        self.assertIs(compiler.compile(compiled), compiled)

    def test_artifact_manager_is_resolved_lazily(self) -> None:
        holder = {"manager": None}
        compiler = ChannelIngressCompiler(
            artifact_manager_provider=lambda: holder["manager"]
        )
        holder["manager"] = _Artifacts()
        compiled = compiler.compile(_envelope({"attachments": [{}]}))
        self.assertEqual(compiled.event.payload.parts[0].artifact_id, "art-1")

    def test_artifact_ref_snapshot_round_trips_but_request_rejects_it(self) -> None:
        message = LLMMessageIR(
            role=MessageRole.USER,
            parts=(TextPartIR("see"), ArtifactRefPartIR("art-1", file_name="shot.png")),
        )
        self.assertEqual(message_from_payload(message_to_payload(message)), message)
        with self.assertRaisesRegex(ValueError, "unresolved artifact"):
            LLMRequestIR(
                messages=(message,),
                tools=(),
                policy=GenerationPolicyIR(max_output_tokens=16),
            )

    def test_existing_string_artifact_ref_is_preserved(self) -> None:
        compiled = ChannelIngressCompiler().compile(
            _envelope({"text": "see this", "artifact_refs": ["art-existing"]})
        )
        refs = [part for part in compiled.event.payload.parts if isinstance(part, ArtifactRefPartIR)]
        self.assertEqual([part.artifact_id for part in refs], ["art-existing"])
        self.assertEqual(_artifact_ids_from_payload(compiled.event.payload), ("art-existing",))

    def test_semantic_and_structured_slash_metadata_survive_ingress(self) -> None:
        compiled = ChannelIngressCompiler().compile(
            _envelope(
                {
                    "text": "/model fast",
                    "command_name": "model",
                    "argv": ["fast"],
                    "from_user_id": "42",
                    "source_metadata": {"locale": "zh-CN"},
                    "chat_id": "route-only",
                },
                event_kind=EventKind.SLASH_COMMAND,
            )
        )
        message = compiled.event.payload
        self.assertEqual(dict(message.metadata["control_payload"]), {"command_name": "model", "argv": ("fast",)})
        self.assertEqual(message.metadata["from_user_id"], "42")
        self.assertEqual(message.metadata["source_metadata"]["locale"], "zh-CN")
        self.assertNotIn("chat_id", message.metadata)

    def test_delivery_binding_is_deeply_immutable_and_serializable(self) -> None:
        envelope = _envelope({"text": "hello"})
        endpoint = EndpointConfig(
            "a",
            "socket",
            "a",
            {"retry": [1, UserDict({"delay": 2})]},
        )
        response = ResponseHandle("a", {"nested": {"ids": [1, 2]}})
        source = ChannelEnvelope(envelope.event, endpoint, response)
        binding = TurnDeliveryBinding.from_envelope(source, control_scope_key="scope")

        endpoint.send_policy["retry"][1]["delay"] = 99
        response.reply_target["nested"]["ids"].append(3)
        self.assertEqual(binding.endpoint.send_policy["retry"][1]["delay"], 2)
        self.assertEqual(binding.response_handle.reply_target["nested"]["ids"], (1, 2))
        self.assertEqual(binding.correlation_id, envelope.event.event_id)
        json.dumps(asdict(binding))
        self.assertEqual(deepcopy(binding), binding)
        with self.assertRaises(TypeError):
            binding.response_handle.reply_target["nested"]["ids"] += (3,)


if __name__ == "__main__":
    unittest.main()

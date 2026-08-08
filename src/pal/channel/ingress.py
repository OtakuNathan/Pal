from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Any

from pal.foundation import EventEnvelope
from pal.control.routing import derive_control_scope_key
from pal.llm.ir import ArtifactRefPartIR, LLMMessageIR, MessageRole, TextPartIR
from pal.shared.agent_io import ChannelEnvelope, TurnDeliveryBinding
from pal.shared.payloads import extract_text_from_payload
from pal.shared.enums import EventKind


@dataclass
class ChannelIngressCompiler:
    """Compile provider-normalized input into the exact user-message IR L1 stores."""

    artifact_manager: Any | None = None
    artifact_manager_provider: Callable[[], Any | None] | None = None
    scope_key: str = "pal:resident"

    def compile(self, envelope: ChannelEnvelope) -> ChannelEnvelope:
        if envelope.event.event_kind not in {EventKind.USER_MESSAGE, EventKind.SLASH_COMMAND}:
            return envelope
        payload = envelope.event.payload
        envelope = self._capture_opening_delivery_binding(
            envelope,
            force=not isinstance(payload, LLMMessageIR),
        )
        payload = envelope.event.payload
        if isinstance(payload, LLMMessageIR):
            return envelope

        text = extract_text_from_payload(payload)
        parts: list[Any] = []
        if text:
            parts.append(TextPartIR(text))
        if isinstance(payload, dict):
            parts.extend(self._compile_artifacts(payload, envelope))

        message = LLMMessageIR(
            role=MessageRole.USER,
            parts=tuple(parts),
            semantic_kind="user_request",
            metadata=_compile_message_metadata(payload),
        )
        event = envelope.event
        return ChannelEnvelope(
            event=EventEnvelope(
                event_kind=event.event_kind,
                source_kind=event.source_kind,
                payload=message,
                correlation_id=event.correlation_id,
                created_at=event.created_at,
                event_id=event.event_id,
            ),
            endpoint=envelope.endpoint,
            response_handle=envelope.response_handle,
            opening_delivery_binding=envelope.opening_delivery_binding,
        )

    @staticmethod
    def _capture_opening_delivery_binding(
        envelope: ChannelEnvelope,
        *,
        force: bool,
    ) -> ChannelEnvelope:
        if envelope.opening_delivery_binding is not None and not force:
            return envelope
        payload = envelope.event.payload
        control_scope_key = derive_control_scope_key(
            endpoint_id=envelope.endpoint.endpoint_id,
            channel_kind=envelope.endpoint.channel_kind,
            reply_target=envelope.response_handle.reply_target,
            payload=payload if isinstance(payload, dict) else {},
        )
        return ChannelEnvelope(
            event=envelope.event,
            endpoint=envelope.endpoint,
            response_handle=envelope.response_handle,
            opening_delivery_binding=TurnDeliveryBinding.from_envelope(
                envelope,
                control_scope_key=control_scope_key,
            ),
        )

    def _compile_artifacts(self, payload: dict[str, Any], envelope: ChannelEnvelope) -> list[ArtifactRefPartIR]:
        refs: list[Any] = list(payload.get("artifact_refs") or ())
        manager = self.artifact_manager_provider() if self.artifact_manager_provider is not None else self.artifact_manager
        register = getattr(manager, "register_ingested", None)
        for index, attachment in enumerate(payload.get("attachments") or ()):
            if callable(register):
                try:
                    refs.append(register(
                        attachment,
                        scope_key=self.scope_key,
                        turn_id=envelope.event.event_id,
                        source_channel=envelope.endpoint.channel_kind,
                        metadata={
                            "source_text": str(payload.get("text") or ""),
                            "caption": str(payload.get("caption") or payload.get("text") or ""),
                            "endpoint_id": envelope.endpoint.endpoint_id,
                        },
                    ))
                except Exception as exc:
                    # Preserve a visible typed placeholder even when artifact
                    # registration itself fails; ingress information must not
                    # disappear merely because storage is unavailable.
                    source = attachment if isinstance(attachment, dict) else {}
                    refs.append({
                        "artifact_id": f"unavailable:{envelope.event.event_id}:{index}",
                        "kind": str(source.get("kind") or "unknown"),
                        "file_name": str(source.get("file_name") or source.get("name") or ""),
                        "summary": f"artifact registration failed: {exc.__class__.__name__}",
                        "status": "failed",
                        "available_actions": [],
                    })
            else:
                source = attachment if isinstance(attachment, dict) else {}
                refs.append({
                    "artifact_id": f"unavailable:{envelope.event.event_id}:{index}",
                    "kind": str(source.get("kind") or "unknown"),
                    "file_name": str(source.get("file_name") or source.get("name") or ""),
                    "summary": "artifact manager unavailable",
                    "status": "failed",
                    "available_actions": [],
                })
        return [_artifact_part(ref) for ref in refs if _artifact_id(ref)]


def _artifact_id(ref: Any) -> str:
    if isinstance(ref, str):
        return ref.strip()
    if isinstance(ref, dict):
        return str(ref.get("artifact_id") or "").strip()
    return str(getattr(ref, "artifact_id", "") or "").strip()


def _artifact_value(ref: Any, name: str, default: Any = "") -> Any:
    if isinstance(ref, str):
        return default
    return ref.get(name, default) if isinstance(ref, dict) else getattr(ref, name, default)


def _artifact_part(ref: Any) -> ArtifactRefPartIR:
    return ArtifactRefPartIR(
        artifact_id=_artifact_id(ref),
        kind=str(_artifact_value(ref, "kind")),
        file_name=str(_artifact_value(ref, "file_name")),
        summary=str(_artifact_value(ref, "summary")),
        status=str(_artifact_value(ref, "status")),
        available_actions=tuple(str(item) for item in (_artifact_value(ref, "available_actions", ()) or ())),
    )


def _compile_message_metadata(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    metadata: dict[str, Any] = {}
    for key in ("from_user_id", "source_metadata"):
        value = payload.get(key)
        if value not in (None, "", {}, []):
            metadata[key] = _json_safe(value)
    control_payload = {
        key: _json_safe(payload[key])
        for key in ("command_name", "command", "argv", "raw_text")
        if key in payload
    }
    if control_payload:
        metadata["control_payload"] = control_payload
    return metadata


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    return str(value)

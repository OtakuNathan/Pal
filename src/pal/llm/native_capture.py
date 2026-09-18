"""Capture native continuation material before response hooks drop it.

The DeepSeek response hook projects every update with ``replay=None``
(deepseek_response._project_response), so the semantic stream loses the wire
envelope at the hook boundary.  The codec's own decode output still carries
it (ResponseIRBuilder attaches the ReplayEnvelope to every snapshot).
``NativeCapture`` is a pass-through iterator placed BETWEEN the codec decode
and the response hooks: semantics flow through unchanged while the final
native snapshot is recorded for the shared runtime (PLAN §6.2: necessary
native must be captured before the normalizer discards information).
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from pal.llm.ir import LLMResponseUpdate, ReplayEnvelope, WireShape
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolCallIR

from pal.llm.continuation_policy import NativeCandidate

__all__ = ["NativeCapture", "decode_with_capture"]


class NativeCapture:
    """Pass-through update iterator that records the last native snapshot."""

    def __init__(
        self,
        updates: Iterable[LLMResponseUpdate],
        *,
        endpoint_id: str,
        model_id: str,
    ) -> None:
        self._updates = iter(updates)
        self._endpoint_id = str(endpoint_id)
        self._model_id = str(model_id)
        self._final_replay: ReplayEnvelope | None = None
        self._final_shape: WireShape | None = None
        self._call_ids: tuple[str, ...] = ()
        self._consumed = False

    def __iter__(self) -> Iterator[LLMResponseUpdate]:
        for update in self._updates:
            self._observe(update)
            yield update
        self._consumed = True

    def _observe(self, update: LLMResponseUpdate) -> None:
        message = update.response.message
        # Only the codec-level envelope counts as native; hooks re-emit
        # snapshots with replay=None, which never overwrites a capture.
        replay = message.replay
        if replay is not None and replay.payload:
            self._final_replay = replay
            self._final_shape = replay.wire_shape
        elif self._final_replay is None:
            # Track codec-level tool inventory even when no envelope exists
            # yet, so the final call order reflects the codec's view.
            pass
        calls = tuple(
            part.call_id for part in message.parts if isinstance(part, ToolCallIR)
        )
        if calls:
            self._call_ids = calls

    def result(self) -> NativeCandidate | None:
        """The captured native candidate, or None when the attempt had none.

        Must be called after the iterator was fully consumed; the capture is
        the codec's LAST envelope snapshot (the completed response).
        """

        if not self._consumed:
            raise RuntimeError("native capture result requested before iteration finished")
        if self._final_replay is None or not self._final_replay.payload:
            return None
        payload_json = json.dumps(
            thaw_json(dict(self._final_replay.payload)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return NativeCandidate(
            wire_shape=WireShape(self._final_shape),
            endpoint_id=self._endpoint_id,
            model_id=self._model_id,
            payload_json=payload_json,
            call_ids=self._call_ids,
        )


def decode_with_capture(
    codec_updates: Iterable[LLMResponseUpdate],
    *,
    endpoint_id: str,
    model_id: str,
) -> NativeCapture:
    """Wrap codec decode output for native capture (pre-hook position)."""

    return NativeCapture(
        codec_updates,
        endpoint_id=endpoint_id,
        model_id=model_id,
    )

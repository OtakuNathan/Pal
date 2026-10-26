"""Preserve raw wire evidence before response hooks adapt response blocks.

The captured envelope supplies original opaque fields when accepted replay is
bound, and remains diagnostic evidence. It never independently authorizes a
projection commit. Continuation derives from the accepted message's replay,
after protocol adaptation and semantic validation.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Generic, TypeVar

from pal.llm.ir import LLMResponseUpdate, ReplayEnvelope, WireShape
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolCallIR
from pal.llm.response_blocks import ResponseBlock, ResponseBlockUpdate, materialize_replay

from pal.llm.continuation_policy import NativeCandidate

__all__ = ["NativeCapture", "decode_with_capture"]


UpdateT = TypeVar("UpdateT", LLMResponseUpdate, ResponseBlockUpdate)


class NativeCapture(Generic[UpdateT]):
    """Pass-through update iterator that records the last native snapshot."""

    def __init__(
        self,
        updates: Iterable[UpdateT],
        *,
        endpoint_id: str,
        model_id: str,
    ) -> None:
        self._updates: Iterator[UpdateT] = iter(updates)
        self._endpoint_id = str(endpoint_id)
        self._model_id = str(model_id)
        self._final_replay: ReplayEnvelope | None = None
        self._call_ids: tuple[str, ...] = ()
        self._consumed = False

    def __iter__(self) -> Iterator[UpdateT]:
        for update in self._updates:
            self._observe(update)
            yield update
        self._consumed = True

    def _observe(self, update: UpdateT) -> None:
        message = update.response.message
        # Only the codec-level envelope counts as native; hooks re-emit
        # snapshots with replay=None, which never overwrites a capture.
        replay = message.replay
        if replay is not None and replay.payload:
            self._final_replay = replay
        elif self._final_replay is None:
            # Track codec-level tool inventory even when no envelope exists
            # yet, so the final call order reflects the codec's view.
            pass
        calls = tuple(
            part.call_id for part in message.parts if isinstance(part, (ToolCallIR, ResponseBlock))
        )
        if calls:
            self._call_ids = calls

    @property
    def replay(self) -> ReplayEnvelope | None:
        """Latest immutable decoder snapshot, before provider normalization."""
        return materialize_replay(self._final_replay)

    def result(self) -> NativeCandidate | None:
        """The captured native candidate, or None when the attempt had none.

        Must be called after the iterator was fully consumed; the capture is
        the codec's LAST envelope snapshot (the completed response).
        """

        if not self._consumed:
            raise RuntimeError("native capture result requested before iteration finished")
        replay = self.replay
        if replay is None or not replay.payload:
            return None
        payload_json = json.dumps(
            thaw_json(dict(replay.payload)),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return NativeCandidate(
            wire_shape=replay.wire_shape,
            endpoint_id=self._endpoint_id,
            model_id=self._model_id,
            payload_json=payload_json,
            call_ids=self._call_ids,
        )


def decode_with_capture(
    codec_updates: Iterable[UpdateT],
    *,
    endpoint_id: str,
    model_id: str,
) -> NativeCapture[UpdateT]:
    """Wrap codec decode output for native capture (pre-hook position)."""

    return NativeCapture(
        codec_updates,
        endpoint_id=endpoint_id,
        model_id=model_id,
    )

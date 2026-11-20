"""Process-local references to accepted provider prefixes. No history content is retained."""
from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from itertools import chain
from threading import RLock
from typing import Any

from pal.llm.ir import LLMMessageIR
from pal.shared.json_values import thaw_json


def _canonical(value: Any) -> bytes:
    return json.dumps(thaw_json(value), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _prefix_digests(payload: Mapping, items: Iterable, lengths: set[int]) -> dict[int, bytes]:
    # Frame each JSON value independently so a changed early item invalidates
    # all later references in a single pass. JSON strings escape newlines.
    state = sha256(_canonical([
        payload.get("system_instruction"),
        payload.get("instructions"),
        payload.get("tools"),
    ]))
    digests = {}
    last = max(lengths)
    for length, item in enumerate(items, start=1):
        state.update(b"\n")
        state.update(_canonical(item))
        if length in lengths:
            digests[length] = state.digest()
        if length == last:
            break
    return digests


@dataclass(frozen=True)
class ResponsePrefix:
    response_id: str
    length: int
    digest: bytes


@dataclass
class ResponseLineage:
    prefixes: OrderedDict = field(default_factory=OrderedDict)
    lock: RLock = field(default_factory=RLock)

    def remember(self, scope, message_id: str, response_id: str, payload: Mapping, output: Sequence) -> None:
        items = payload.get("input")
        if not items or not output or not response_id:
            return
        length = len(items) + len(output)
        digest = _prefix_digests(payload, chain(items, output), {length})[length]
        key = (scope, message_id)
        prefix = ResponsePrefix(response_id, length, digest)
        with self.lock:
            self.prefixes[key] = prefix
            self.prefixes.move_to_end(key)
            if len(self.prefixes) > 256:
                self.prefixes.popitem(last=False)

    def match(self, scope, messages: Sequence[LLMMessageIR], payload: Mapping) -> ResponsePrefix | None:
        items = payload.get("input", ())
        with self.lock:
            candidates = [
                self.prefixes.get((scope, message.message_id))
                for message in reversed(messages)
                if message.replay and message.replay.source_payload is None
            ]
        eligible = [prefix for prefix in candidates if prefix and prefix.length < len(items)]
        if not eligible:
            return None
        digests = _prefix_digests(payload, items, {prefix.length for prefix in eligible})
        return max((prefix for prefix in eligible if prefix.digest == digests.get(prefix.length)),
                   key=lambda prefix: prefix.length, default=None)

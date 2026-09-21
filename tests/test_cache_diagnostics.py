"""Bounded cache diagnostics describe wire bytes, not span coverage.

PLAN §7 / I11: the only local truth is the outbound wire.  A derived
projection re-encodes only the seam and the tail (§6.2: the stable prefix
is never rewritten), so frozen-prefix messages keep their wire bytes while
their spans do not travel with the assembled request.  The wire
description must account for those bytes from the conversation container
itself instead of dropping every message whose span is absent, otherwise
consecutive rounds compare span coverage rather than bytes and report
phantom ``prefix_changed`` alarms.
"""
from types import SimpleNamespace

from pal.llm.cache_diagnostics import compare_requests, describe_request
from pal.llm.ir import PromptRegionIR
from pal.llm.shapes.base import EncodedMessageSpan, EncodedRequest


def _message(message_id: str, region: PromptRegionIR):
    return SimpleNamespace(message_id=message_id, prompt_region=region)


def _item(name: str) -> dict:
    return {"id": name, "type": "message", "content": f"wire item {name}"}


def _encoded(payload: dict, spans) -> EncodedRequest:
    return EncodedRequest(payload=payload, message_spans=tuple(spans))


_MESSAGES = (
    _message("m-sys", PromptRegionIR.STABLE_SYSTEM),
    _message("m-h1", PromptRegionIR.SETTLED_HISTORY),
    _message("m-h2", PromptRegionIR.SETTLED_HISTORY),
    _message("m-dyn", PromptRegionIR.ACTIVE_DYNAMIC),
)

_COLD_SPANS = (
    EncodedMessageSpan("m-sys", wire_item_paths=(("input", 0),)),
    EncodedMessageSpan("m-h1", wire_item_paths=(("input", 1),)),
    EncodedMessageSpan("m-h2", wire_item_paths=(("input", 2),)),
    EncodedMessageSpan("m-dyn", wire_item_paths=(("input", 3),)),
)

# Projected round: the shell preamble and the freshly encoded tail carry
# spans; the frozen history keeps its wire bytes without a travelling span.
_PROJECTED_SPANS_GROWN_TAIL = (
    EncodedMessageSpan("m-sys", wire_item_paths=(("input", 0),)),
    EncodedMessageSpan("m-dyn", wire_item_paths=(("input", 3), ("input", 4))),
)
_PROJECTED_SPANS = (
    EncodedMessageSpan("m-sys", wire_item_paths=(("input", 0),)),
    EncodedMessageSpan("m-dyn", wire_item_paths=(("input", 3),)),
)


def test_projected_round_still_accounts_for_frozen_prefix_wire_items():
    payload_round1 = {"input": [_item(n) for n in ("sys", "h1", "h2", "dyn-1")]}
    payload_round2 = {"input": [_item(n) for n in ("sys", "h1", "h2", "dyn-1", "dyn-2")]}
    round1 = describe_request(SimpleNamespace(messages=list(_MESSAGES)),
                              _encoded(payload_round1, _COLD_SPANS), _encoded(payload_round1, _COLD_SPANS))
    round2 = describe_request(SimpleNamespace(messages=list(_MESSAGES)),
                              _encoded(payload_round2, _PROJECTED_SPANS_GROWN_TAIL),
                              _encoded(payload_round2, _PROJECTED_SPANS_GROWN_TAIL))
    non_dynamic = [item for item in round2["_items"] if item[0] != "dynamic"]
    assert len(non_dynamic) == 3  # sys + h1 + h2 described from the wire
    changes = compare_requests(round1, round2)
    assert changes["prefix_preserved"] is True
    assert changes["change_reason"] != "prefix_changed"


def test_frozen_prefix_byte_drift_without_spans_is_still_detected():
    payload_a = {"input": [_item(n) for n in ("sys", "h1", "h2", "dyn-1")]}
    payload_b = {"input": [_item(n) for n in ("sys", "h1-MUTATED", "h2", "dyn-1")]}
    base = describe_request(SimpleNamespace(messages=list(_MESSAGES)),
                            _encoded(payload_a, _COLD_SPANS), _encoded(payload_a, _COLD_SPANS))
    drifted = describe_request(SimpleNamespace(messages=list(_MESSAGES)),
                               _encoded(payload_b, _PROJECTED_SPANS), _encoded(payload_b, _PROJECTED_SPANS))
    changes = compare_requests(base, drifted)
    assert changes["prefix_preserved"] is False
    assert changes["change_reason"] == "prefix_changed"

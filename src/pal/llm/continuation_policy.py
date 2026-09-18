"""Structural continuation contracts per wire shape (PLAN §7).

Two rules govern this module:

1. Contracts are STRUCTURAL, not brand-derived: they validate properties of
   the decoded native payload (signatures present, inventories associated,
   nothing silently dropped).  They never guess required fields from a
   provider name; per-model behavior differences belong to endpoint spec
   configuration, not here.
2. Validation outcomes are explicit: Preserved / Degraded / Unsupported.
   A missing required continuation field must surface as Degraded (an
   explicit decision point), never as a silent fallback to semantic-only
   re-encoding.

The contracts below cover what the current fleet actually emits (see
docs/llm_projection_refactor/continuation_contract_matrix.md).  Unknown
payload shapes are Unsupported, not ignored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

from pal.llm.ir import WireShape

__all__ = [
    "ContinuationDecisionKind",
    "ContinuationIssue",
    "ContinuationDecision",
    "NativeCandidate",
    "ContinuationContract",
    "contract_for_shape",
    "validate_candidate",
]

_ANTHROPIC_CONTRACT_VERSION = "anthropic-structural-1"
_OPENAI_RESPONSE_CONTRACT_VERSION = "openai-response-structural-1"
_OPENAI_COMPLETION_CONTRACT_VERSION = "openai-completion-verbatim-1"


class ContinuationDecisionKind(Enum):
    PRESERVED = "preserved"
    DEGRADED = "degraded"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ContinuationIssue:
    code: str
    detail: str


@dataclass(frozen=True)
class ContinuationDecision:
    kind: ContinuationDecisionKind
    issues: tuple[ContinuationIssue, ...] = ()
    contract_version: str = ""

    @property
    def ok(self) -> bool:
        return self.kind is ContinuationDecisionKind.PRESERVED


@dataclass(frozen=True)
class NativeCandidate:
    """Native continuation material captured from one decode attempt.

    ``payload_json`` is the canonical serialization of the provider-native
    payload (the replay envelope payload).  ``call_ids`` is the codec-level
    tool-call inventory, in order of appearance; the shared runtime
    reconciles it against the ACCEPTED call set before any association is
    trusted (a response hook may promote textual DSML into structured calls
    the codec never saw).
    """

    wire_shape: WireShape
    endpoint_id: str
    model_id: str
    payload_json: str
    call_ids: tuple[str, ...]

    def payload(self) -> Mapping[str, Any]:
        return json.loads(self.payload_json)


def _issue(code: str, detail: str) -> ContinuationIssue:
    return ContinuationIssue(code=code, detail=detail)


def _validate_anthropic(candidate: NativeCandidate) -> ContinuationDecision:
    issues: list[ContinuationIssue] = []
    payload = candidate.payload()
    if not isinstance(payload, Mapping):
        return ContinuationDecision(
            ContinuationDecisionKind.UNSUPPORTED,
            (_issue("payload_not_object", "anthropic payload must be an object"),),
            _ANTHROPIC_CONTRACT_VERSION,
        )
    blocks = payload.get("content")
    if not isinstance(blocks, (list, tuple)):
        return ContinuationDecision(
            ContinuationDecisionKind.UNSUPPORTED,
            (_issue("content_missing", "anthropic payload has no content blocks"),),
            _ANTHROPIC_CONTRACT_VERSION,
        )
    tool_ids: list[str] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            issues.append(_issue("block_not_object", f"content[{index}] is not an object"))
            continue
        block_type = str(block.get("type") or "")
        if block_type == "thinking":
            # [W2] thinking blocks must round-trip unmodified, including the
            # signature.  A thinking block without a signature cannot be
            # replayed legally: that is Degraded, not silently-strip-able.
            if not str(block.get("signature") or "").strip():
                issues.append(
                    _issue(
                        "thinking_missing_signature",
                        f"content[{index}] thinking block has no signature",
                    )
                )
        elif block_type == "redacted_thinking":
            # The opaque block is its own proof; nothing structural to check.
            continue
        elif block_type == "tool_use":
            call_id = str(block.get("id") or "").strip()
            if not call_id:
                issues.append(
                    _issue("tool_use_missing_id", f"content[{index}] tool_use has no id")
                )
            elif not str(block.get("name") or "").strip():
                issues.append(
                    _issue("tool_use_missing_name", f"content[{index}] tool_use has no name")
                )
            else:
                tool_ids.append(call_id)
        elif block_type in {"text"}:
            continue
        else:
            # PLAN §7.3: unknown blocks must not be silently dropped.
            issues.append(
                _issue("unknown_block_type", f"content[{index}] has unknown type {block_type!r}")
            )
    # Inventory equality is unconditional in BOTH directions: a wire with
    # no calls paired with a candidate that claims calls is just as wrong as
    # the reverse.  A truthy-left shortcut would let an empty wire inventory
    # silently mask a non-empty semantic inventory (review R8).
    if tuple(tool_ids) != candidate.call_ids:
        issues.append(
            _issue(
                "tool_inventory_mismatch",
                f"tool_use ids {tool_ids} do not match codec inventory {list(candidate.call_ids)}",
            )
        )
    kind = (
        ContinuationDecisionKind.PRESERVED
        if not issues
        else ContinuationDecisionKind.DEGRADED
    )
    return ContinuationDecision(kind, tuple(issues), _ANTHROPIC_CONTRACT_VERSION)


def _validate_openai_response(candidate: NativeCandidate) -> ContinuationDecision:
    issues: list[ContinuationIssue] = []
    payload = candidate.payload()
    if not isinstance(payload, Mapping):
        return ContinuationDecision(
            ContinuationDecisionKind.UNSUPPORTED,
            (_issue("payload_not_object", "response payload must be an object"),),
            _OPENAI_RESPONSE_CONTRACT_VERSION,
        )
    output = payload.get("output")
    if not isinstance(output, (list, tuple)):
        return ContinuationDecision(
            ContinuationDecisionKind.UNSUPPORTED,
            (_issue("output_missing", "response payload has no output items"),),
            _OPENAI_RESPONSE_CONTRACT_VERSION,
        )
    call_ids: list[str] = []
    for index, item in enumerate(output):
        if not isinstance(item, Mapping):
            issues.append(_issue("item_not_object", f"output[{index}] is not an object"))
            continue
        item_type = str(item.get("type") or "")
        if item_type == "reasoning":
            # [W1] stateless continuation relies on encrypted_content; a
            # reasoning item without it cannot preserve the reasoning chain.
            if not str(item.get("encrypted_content") or "").strip():
                issues.append(
                    _issue(
                        "reasoning_missing_encrypted_content",
                        f"output[{index}] reasoning item has no encrypted_content",
                    )
                )
        elif item_type == "function_call":
            call_id = str(item.get("call_id") or "").strip()
            if not call_id or not str(item.get("name") or "").strip():
                issues.append(
                    _issue("function_call_incomplete", f"output[{index}] function_call lacks call_id/name")
                )
            else:
                call_ids.append(call_id)
        elif item_type in {"message", "summary"}:
            continue
        else:
            issues.append(
                _issue("unknown_item_type", f"output[{index}] has unknown type {item_type!r}")
            )
    if tuple(call_ids) != candidate.call_ids:
        issues.append(
            _issue(
                "tool_inventory_mismatch",
                f"function_call ids {call_ids} do not match codec inventory {list(candidate.call_ids)}",
            )
        )
    kind = (
        ContinuationDecisionKind.PRESERVED
        if not issues
        else ContinuationDecisionKind.DEGRADED
    )
    return ContinuationDecision(kind, tuple(issues), _OPENAI_RESPONSE_CONTRACT_VERSION)


def _validate_openai_completion(candidate: NativeCandidate) -> ContinuationDecision:
    issues: list[ContinuationIssue] = []
    payload = candidate.payload()
    if not isinstance(payload, Mapping):
        return ContinuationDecision(
            ContinuationDecisionKind.UNSUPPORTED,
            (_issue("payload_not_object", "completion payload must be an object"),),
            _OPENAI_COMPLETION_CONTRACT_VERSION,
        )
    message = payload.get("message")
    if not isinstance(message, Mapping):
        return ContinuationDecision(
            ContinuationDecisionKind.UNSUPPORTED,
            (_issue("message_missing", "completion payload has no message object"),),
            _OPENAI_COMPLETION_CONTRACT_VERSION,
        )
    # GLM-family requirements are unverified (matrix TBD-P2); the contract is
    # verbatim preservation only: reasoning_content and tool_calls must stay
    # byte-identical in the payload, which capture guarantees structurally.
    tool_calls = message.get("tool_calls")
    call_ids: list[str] = []
    if isinstance(tool_calls, (list, tuple)):
        for index, call in enumerate(tool_calls):
            if not isinstance(call, Mapping):
                issues.append(_issue("tool_call_not_object", f"tool_calls[{index}] is not an object"))
                continue
            call_id = str(call.get("id") or "").strip()
            function = call.get("function")
            if not call_id or not isinstance(function, Mapping) or not str(
                function.get("name") or ""
            ).strip():
                issues.append(
                    _issue("tool_call_incomplete", f"tool_calls[{index}] lacks id/function.name")
                )
            else:
                call_ids.append(call_id)
    if tuple(call_ids) != candidate.call_ids:
        issues.append(
            _issue(
                "tool_inventory_mismatch",
                f"tool_calls ids {call_ids} do not match codec inventory {list(candidate.call_ids)}",
            )
        )
    kind = (
        ContinuationDecisionKind.PRESERVED
        if not issues
        else ContinuationDecisionKind.DEGRADED
    )
    return ContinuationDecision(kind, tuple(issues), _OPENAI_COMPLETION_CONTRACT_VERSION)


@dataclass(frozen=True)
class ContinuationContract:
    """Structural validation rule for one wire shape."""

    wire_shape: WireShape
    contract_version: str
    validator: Any  # Callable[[NativeCandidate], ContinuationDecision]

    def validate(self, candidate: NativeCandidate) -> ContinuationDecision:
        if candidate.wire_shape is not self.wire_shape:
            return ContinuationDecision(
                ContinuationDecisionKind.UNSUPPORTED,
                (
                    _issue(
                        "shape_mismatch",
                        f"candidate shape {candidate.wire_shape.value} does not match contract",
                    ),
                ),
                self.contract_version,
            )
        return self.validator(candidate)


_CONTRACTS: dict[WireShape, ContinuationContract] = {
    WireShape.ANTHROPIC_MESSAGES: ContinuationContract(
        WireShape.ANTHROPIC_MESSAGES,
        _ANTHROPIC_CONTRACT_VERSION,
        _validate_anthropic,
    ),
    WireShape.OPENAI_RESPONSE: ContinuationContract(
        WireShape.OPENAI_RESPONSE,
        _OPENAI_RESPONSE_CONTRACT_VERSION,
        _validate_openai_response,
    ),
    WireShape.OPENAI_COMPLETION: ContinuationContract(
        WireShape.OPENAI_COMPLETION,
        _OPENAI_COMPLETION_CONTRACT_VERSION,
        _validate_openai_completion,
    ),
}


def contract_for_shape(shape: WireShape) -> ContinuationContract | None:
    """Return the structural contract for a shape, or None when unsupported."""

    return _CONTRACTS.get(WireShape(shape))


def validate_candidate(candidate: NativeCandidate) -> ContinuationDecision:
    contract = contract_for_shape(candidate.wire_shape)
    if contract is None:
        return ContinuationDecision(
            ContinuationDecisionKind.UNSUPPORTED,
            (_issue("no_contract", "no continuation contract for this wire shape"),),
        )
    return contract.validate(candidate)

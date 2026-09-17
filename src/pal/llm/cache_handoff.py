"""Conservative explicit-cache handoff; no provider truth enters the controller.

All coordinates here are serialization estimates. Usage counts are evidence,
never substituted for those coordinates. The coordinator serializes access.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

from pal.llm.ir import LLMUsageIR
from pal.llm.shapes.base import EncodedRequest, finalize_cache_spans
from pal.shared.json_values import thaw_json

POLICY = "openai_explicit_economic_v1"
CONTROL_FIELDS = ("prompt_cache_breakpoint", "cache_control")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(thaw_json(value), ensure_ascii=False, separators=(",", ":"),
                                     sort_keys=True).encode()).hexdigest()


def at(payload: dict, path: tuple) -> Any:
    try:
        for part in path:
            payload = payload[part]
        return payload
    except (KeyError, IndexError, TypeError):
        return None


def protocol_nodes(payload: dict):
    """Visit wire envelopes only, never tool arguments, schema or user data."""
    yield (), payload
    for root in ("input", "messages", "system", "tools"):
        items = payload.get(root)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            yield (root, i), item
            if root == "tools":
                continue
            for field_name in ("content", "output"):
                blocks = item.get(field_name)
                if isinstance(blocks, list):
                    for j, block in enumerate(blocks):
                        if isinstance(block, dict):
                            yield (root, i, field_name, j), block


def clean_request(encoded: EncodedRequest) -> EncodedRequest:
    """Normalize known controls before content hashing, including extra_body.

    The transport merges extra_body over payload. Plan against that same input,
    so an override of messages/tools cannot silently invalidate our audit.
    """
    payload = thaw_json(encoded.payload)
    payload.update(thaw_json(encoded.extra_body))
    for _, node in protocol_nodes(payload):
        for key in CONTROL_FIELDS:
            node.pop(key, None)
    for key in ("prompt_cache_options", "prompt_cache_key", "session_id"):
        payload.pop(key, None)
    # Chat instruction strings need a concrete content block before fingerprinting.
    for item in payload.get("messages", ()):
        if isinstance(item, dict) and isinstance(item.get("content"), str):
            item["content"] = [{"type": "text", "text": item["content"]}]
    spans = []
    for span in encoded.message_spans:
        paths = tuple(path + ("content", 0) if len(path) == 2
                      and path[0] == "messages" and isinstance(at(payload, path), dict)
                      and isinstance(at(payload, path).get("content"), list)
                      else path for path in span.cache_targets)
        spans.append(replace(span, cache_targets=paths))
    return finalize_cache_spans(EncodedRequest(payload, tuple(spans)))


def inject_exact(encoded: EncodedRequest, points: tuple, cache_key: str,
                 gateway: bool, *, prepared: bool = False) -> EncodedRequest:
    clean = encoded if prepared else clean_request(encoded)
    payload = thaw_json(clean.payload)
    spans = {s.message_id: s for s in clean.message_spans}
    applied = []
    for point in points:
        span = spans.get(point.message_id)
        if not span or not span.cache_targets:
            continue
        target = at(payload, span.cache_targets[-1])
        # Only supported content blocks; never fallback to an earlier block.
        if isinstance(target, dict) and target.get("type") in {
            "input_text", "output_text", "text", "input_image", "image_url",
            "input_audio", "file", "input_file", "refusal",
        }:
            target["prompt_cache_breakpoint"] = {"mode": "explicit"}
            applied.append(point.message_id)
    extra = {"prompt_cache_key": cache_key,
             "prompt_cache_options": {"mode": "explicit", "ttl": "30m"}}
    if gateway:
        extra["session_id"] = cache_key
    return EncodedRequest(payload, clean.message_spans, extra, tuple(applied))


def audit(encoded: EncodedRequest, expected: tuple[tuple, ...]) -> tuple[bool, str]:
    payload = thaw_json(encoded.payload)
    payload.update(thaw_json(encoded.extra_body))
    found = []
    valid = payload.get("prompt_cache_options") == {"mode": "explicit", "ttl": "30m"}
    for path, node in protocol_nodes(payload):
        if "cache_control" in node:
            valid = False
        if "prompt_cache_breakpoint" in node:
            found.append(path)
            valid &= node["prompt_cache_breakpoint"] == {"mode": "explicit"}
    valid &= len(found) <= 4 and set(found) == set(expected) and len(found) == len(set(found))
    return bool(valid), digest({"paths": found, "options": payload.get("prompt_cache_options"),
                               "key": payload.get("prompt_cache_key"),
                               "session": payload.get("session_id")})


def suffix_estimate(encoded: EncodedRequest, path: tuple) -> int | None:
    """Estimate only visible suffix; opaque replay/multimodal suffix cannot calibrate."""
    payload = thaw_json(encoded.payload)
    root, i = path[:2]
    items = payload.get(root, [])
    suffix = thaw_json(items[i + 1:])
    if len(path) == 4:
        blocks = items[i].get(path[2], [])
        if blocks[path[3] + 1:]:
            suffix.insert(0, {path[2]: blocks[path[3] + 1:]})
    def visible(value):
        if isinstance(value, dict):
            if value.get("type") not in (None, "message", "input_text", "output_text", "text",
                                         "function_call", "function_call_output"):
                return False
            if any(k in value for k in ("encrypted_content", "image_url", "audio", "file_id")):
                return False
            return all(visible(v) for v in value.values())
        return not isinstance(value, list) or all(visible(v) for v in value)
    if not visible(suffix):
        return None
    return (len(json.dumps(suffix, ensure_ascii=False, separators=(",", ":"))) + 3) // 4 if suffix else 0


@dataclass(frozen=True)
class Boundary:
    message_id: str
    path: tuple
    fingerprint: str
    coordinate: int


@dataclass(frozen=True)
class Bound:
    upper: int
    request_id: str
    version: int


@dataclass
class Candidate:
    boundary: Boundary
    kind: str
    identity: str = field(default_factory=lambda: uuid4().hex)
    attempts: int = 0
    deadline: float = 0
    calibration_sequence: int = 0
    calibration_request: str = ""
    interval: tuple[int, int] | None = None
    calibration_hit: int = 0
    through: int = 0
    after: int = 0
    creation_max_target: int = 0


@dataclass(frozen=True)
class Submission:
    identity: str
    sequence: int
    owner: int
    economic_epoch: int
    candidate: str
    markers: tuple[Boundary, ...]
    bounds: tuple[tuple[str, Bound | None], ...]
    m: int | None
    target: int
    suffix: int | None
    round_key: tuple[str, int]
    audited: bool
    wire_fingerprint: str


@dataclass
class Handoff:
    stable: Boundary | None = None
    anchor: Boundary | None = None
    frontier: Boundary | None = None
    pending: Candidate | None = None
    bounds: dict[str, Bound] = field(default_factory=dict)
    owner: int = 0
    turn: str = ""
    closed: bool = False
    economic_epoch: int = 0
    sequence: int = 0
    r: int = 0
    max_target: int = 0
    extent: Boundary | None = None
    active: dict[str, Submission] = field(default_factory=dict)
    cooldown: set[tuple[str, int]] | None = None
    completed_round: int = -1
    cooldown_owner: int = 0
    cooldown_floor: int = -1
    last_evidence: dict = field(default_factory=dict)
    decision: str = "bootstrap"
    deferred: tuple[str, int] | None = None
    revision: int = 0
    last_access: float = field(default_factory=time.monotonic)
    actual_provider: str = ""
    returned_model: str = ""
    service_tier: str = ""
    binding_sequence: int = 0
    stable_read: bool = False
    ack_source: str = ""
    promotions: int = 0

    @property
    def baseline(self) -> int:
        return max((b.coordinate for b in (self.stable, self.anchor, self.frontier)
                    if b and (b != self.stable or self.stable_read)), default=0)

    def markers(self) -> tuple[Boundary, ...]:
        points = (self.stable, self.anchor, self.frontier,
                  self.pending.boundary if self.pending else None)
        return tuple({b.path: b for b in points if b}.values())

    def end_candidate(self, reason: str, *, cooldown: bool = False):
        if self.pending:
            self.r = self.pending.through + self.pending.after
        self.pending = None
        self.decision = reason
        if cooldown:
            self.cooldown = set()
            self.cooldown_owner = self.owner
            self.cooldown_floor = self.completed_round

    def end_turn(self, turn: str):
        if self.turn != turn or self.closed:
            return
        self.owner += 1
        self.closed = True
        if self.pending and (self.pending.kind != "anchor" or self.pending.attempts >= 3):
            self.end_candidate("turn_ended", cooldown=self.pending.attempts >= 3)

    def refresh(self, turn: str, boundaries: dict[str, Boundary], stable: Boundary | None,
                now: float):
        self.last_access = now
        if turn != self.turn or self.closed:
            self.end_turn(self.turn)
            self.turn, self.closed = turn, False
            self.owner += 1
            self.completed_round = -1
        def retained(b):
            if not b:
                return None
            # Hash includes exact prefix; message IDs alone never preserve evidence.
            return next((v for v in boundaries.values() if v.fingerprint == b.fingerprint
                         and v.path == b.path), None)
        reset_economics = False
        new_stable = retained(self.stable)
        if self.stable and (not new_stable or stable is None
                            or stable.fingerprint != self.stable.fingerprint
                            or stable.path != self.stable.path):
            self.end_candidate("stable_prefix_invalidated")
            self.anchor = self.frontier = None
            self.bounds.clear()
            self.stable_read = False
            self.ack_source = ""
            reset_economics = True
        self.stable = stable
        for name in ("anchor", "frontier"):
            old = getattr(self, name)
            new = retained(old)
            if old and not new:
                self.end_candidate("prefix_invalidated")
                reset_economics = True
            setattr(self, name, new)
        if self.pending:
            b = retained(self.pending.boundary)
            if b is None:
                self.end_candidate("candidate_prefix_invalidated")
            else:
                self.pending.boundary = b
                if self.pending.deadline and now >= self.pending.deadline:
                    self.end_candidate("candidate_expired", cooldown=True)
        if reset_economics or (self.extent and not retained(self.extent)):
            self.economic_epoch += 1
            self.r = self.max_target = 0
            self.extent = None
            if self.pending:
                self.pending.through = self.pending.after = 0
        # Evidence storage is bounded by current protected points and one pending.
        keep = {b.fingerprint for b in self.markers()}
        self.bounds = {k: v for k, v in self.bounds.items() if k in keep}

    def propose(self, boundary: Boundary | None, kind: str, *, minimum: int,
                read: float, write: float, threshold: int):
        if self.pending or self.closed or self.cooldown is not None or boundary is None:
            return
        if any(b.fingerprint not in self.bounds for b in self.markers()):
            self.decision = "competing_bound_unknown"
            return
        if self.deferred == (boundary.fingerprint, self.revision):
            return
        b, c = self.baseline, boundary.coordinate
        if c <= b or c < minimum or c < self.max_target:
            self.decision = "await_farther_candidate"
            return
        delta = c - b
        net = (self.r + delta) * (1 - read) - delta * max(write - 1, 0)
        if net < threshold:
            self.decision = "accumulate"
            return
        self.pending = Candidate(boundary, kind, through=self.r,
                                 creation_max_target=self.max_target)
        self.r = 0
        self.decision = "candidate_proposed"

    def submit(self, identity: str, *, target: Boundary | None, suffix: int | None,
               round_index: int, audited: bool, wire_fingerprint: str, now: float,
               candidate_id: str | None = None, points: tuple[Boundary, ...] | None = None,
               owner: int | None = None, economic_epoch: int | None = None):
        if identity in self.active:
            return
        self.sequence += 1
        candidate = self.pending
        if candidate_id is not None and (candidate is None or candidate_id != candidate.identity):
            candidate = None
        points = self.markers() if points is None else points
        competitors = tuple((b.fingerprint, self.bounds.get(b.fingerprint)) for b in points
                            if not candidate or b.path != candidate.boundary.path)
        m = max((e.upper for _, e in competitors if e), default=0) if all(
            e is not None for _, e in competitors) else None
        if candidate:
            if candidate.attempts >= 3:
                raise RuntimeError("cache candidate attempt budget exhausted before submission")
            candidate.attempts += 1
            if not candidate.deadline:
                candidate.deadline = now + 1800
        self.active[identity] = Submission(identity, self.sequence, self.owner if owner is None else owner,
            self.economic_epoch if economic_epoch is None else economic_epoch, candidate.identity if candidate else "", points,
            competitors, m, target.coordinate if target else self.baseline, suffix,
            (self.turn, round_index), audited, wire_fingerprint)
        if target and target.coordinate >= self.max_target:
            self.max_target, self.extent = target.coordinate, target

    def settle(self, identity: str, usage: LLMUsageIR, *, success: bool, now: float,
               actual_provider: str = "", returned_model: str = "", service_tier: str = "",
               normal_round: bool = True):
        # Pop is identity-based: old sequence numbers may still be legitimate.
        sent = self.active.pop(identity, None)
        if sent is None:
            return
        candidate = self.pending
        if sent.economic_epoch == self.economic_epoch:
            b, p = self.baseline, sent.target
            if candidate:
                c = candidate.boundary.coordinate
                candidate.through += max(0, min(p, c) - b)
                candidate.after += max(0, p - c)
            else:
                self.r += max(0, p - b)
        current = sent.owner == self.owner and not self.closed
        if success and normal_round and current:
            self.completed_round = max(self.completed_round, sent.round_key[1])
        if (success and normal_round and current and self.cooldown is not None
                and (self.owner > self.cooldown_owner or sent.round_key[1] > self.cooldown_floor)):
            self.cooldown.add(sent.round_key)
            if len(self.cooldown) >= 3:
                self.cooldown = None
        reported_binding = {"actual_provider": actual_provider, "returned_model": returned_model,
                            "service_tier": service_tier}
        binding_mismatch = any(value and getattr(self, key) and value != getattr(self, key)
                               for key, value in reported_binding.items())
        latest_binding = current and sent.sequence >= self.binding_sequence
        changed = latest_binding and binding_mismatch
        if latest_binding:
            self.binding_sequence = sent.sequence
            for key, value in reported_binding.items():
                if value:
                    setattr(self, key, value)
        if changed:
            self.end_candidate("provider_binding_changed")
            self.bounds.clear()
            self.stable_read = False
            self.anchor = self.frontier = None
            self.economic_epoch += 1
            self.r = self.max_target = 0
            self.extent = None
            return
        valid = (success and usage.final and usage.reported and not usage.usage_anomaly
                 and usage.has("input_tokens") and usage.has("cached_input_tokens")
                 and usage.input_accounting == "inclusive_cache"
                 and usage.input_tokens > 0
                 and 0 <= usage.cached_input_tokens <= usage.input_tokens and sent.audited
                 and not (binding_mismatch and not latest_binding))
        self.last_evidence = {"request_id": identity, "send_sequence": sent.sequence,
            "candidate": sent.candidate, "owner": sent.owner, "M": sent.m,
            "suffix_estimate": sent.suffix,
            "markers": [{"message_id": b.message_id, "path": b.path,
                         "fingerprint": b.fingerprint, "coordinate_estimate": b.coordinate}
                        for b in sent.markers],
            "calibration_request": candidate.calibration_request if candidate and candidate.identity == sent.candidate else "",
            "interval": candidate.interval if candidate and candidate.identity == sent.candidate else None,
            "competitors": [{"fingerprint": fp, "upper": e.upper if e else None,
                "source": e.request_id if e else "", "version": e.version if e else None}
                for fp, e in sent.bounds], "audited": sent.audited,
            "wire_fingerprint": sent.wire_fingerprint,
            "input": usage.input_tokens if usage.has("input_tokens") else None,
            "hit": usage.cached_input_tokens if usage.has("cached_input_tokens") else None}
        promoted = False
        h, total = usage.cached_input_tokens, usage.input_tokens
        if candidate and candidate.identity == sent.candidate and current:
            if now >= candidate.deadline:
                self.end_candidate("candidate_expired", cooldown=True)
            elif valid:
                if not candidate.calibration_sequence and sent.suffix is not None:
                    estimate = total - sent.suffix
                    epsilon = max(256, math.ceil(.25 * sent.suffix))
                    candidate.interval = (max(0, estimate - epsilon), min(total, estimate + epsilon))
                    candidate.calibration_sequence = sent.sequence
                    candidate.calibration_request = identity
                    candidate.calibration_hit = h
                    self.decision = "candidate_calibrated"
                    if sent.m is not None and candidate.interval[1] <= sent.m:
                        self.deferred = (candidate.boundary.fingerprint, self.revision)
                        self.end_candidate("candidate_inseparable_from_old_bounds")
                elif (candidate.calibration_sequence and sent.sequence > candidate.calibration_sequence
                      and candidate.interval and sent.m is not None and h > sent.m
                      and candidate.interval[0] <= h <= candidate.interval[1]):
                    if candidate.kind == "anchor":
                        self.anchor = candidate.boundary
                        if self.frontier and self.frontier.coordinate <= self.anchor.coordinate:
                            self.frontier = None
                    else:
                        self.frontier = candidate.boundary
                    self.r = candidate.after
                    self.pending = None
                    self.ack_source = "estimated"
                    self.promotions += 1
                    promoted = True
                    self.decision = "estimated_ack"
                    self._bound(candidate.boundary, h, identity)
                else:
                    self.decision = ("competing_bound_unknown" if sent.m is None else
                                     "read_observed_unattributed" if h else "candidate_reuse_not_observed")
            if self.pending is candidate and candidate.attempts >= 3 and not any(
                    s.candidate == candidate.identity for s in self.active.values()):
                self.end_candidate("candidate_budget_exhausted", cooldown=True)
        # Evidence is learned only AFTER judging this response with frozen M.
        if valid and current:
            active_fingerprints = {b.fingerprint for b in self.markers()}
            for b in sent.markers:
                if b.fingerprint in active_fingerprints:
                    self._bound(b, total, identity)
            if len(sent.markers) == 1 and h > 0:
                b = sent.markers[0]
                if b.fingerprint in active_fingerprints:
                    self._bound(b, h, identity)
                if (self.stable and b.fingerprint == self.stable.fingerprint
                        and not self.stable_read and self.pending is None):
                    self.stable_read = True
                    self.economic_epoch += 1
                    self.r = self.max_target = 0
                    self.extent = None
            # An independently attributed later boundary also bounds earlier ones.
            if promoted:
                for b in self.markers():
                    if b.coordinate <= candidate.boundary.coordinate:
                        self._bound(b, h, identity)

    def _bound(self, boundary: Boundary, upper: int, identity: str):
        old = self.bounds.get(boundary.fingerprint)
        if old is None or upper < old.upper:
            self.revision += 1
            self.bounds[boundary.fingerprint] = Bound(upper, identity, self.revision)

    def snapshot(self):
        c = self.pending
        return {"policy": POLICY, "decision": self.decision, "ack_source": self.ack_source,
                "promotions": self.promotions, "owner_epoch": self.owner,
                "economic_epoch": self.economic_epoch, "send_sequence": self.sequence,
                "baseline_estimate": self.baseline, "reprocessed_estimate": self.r,
                "pending": None if c is None else {"id": c.identity, "kind": c.kind,
                    "message_id": c.boundary.message_id, "path": c.boundary.path,
                    "coordinate_estimate": c.boundary.coordinate,
                    "attempts": c.attempts, "deadline": c.deadline,
                    "calibration_sequence": c.calibration_sequence, "interval": c.interval,
                    "through_estimate": c.through, "after_estimate": c.after},
                "active_attempts": len(self.active), "evidence": self.last_evidence}

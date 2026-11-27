"""One process-local resident cache lease. Heartbeats never enter generation history."""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_FLOOR
from threading import RLock
from typing import Any
import time

from pal.llm.cache_capabilities import KeepaliveProfile
from pal.llm.ir import LLMUsageIR
from pal.llm.shapes.base import EncodedRequest
from pal.shared.json_values import freeze_json_mapping, thaw_json


def action_budget(profile: KeepaliveProfile) -> int:
    ratio = Decimal(str(profile.write_multiplier)) / Decimal(str(profile.read_multiplier))
    return min(19, max(0, int((ratio - 1).to_integral_value(rounding=ROUND_FLOOR)) - 1))


def complete_usage(usage: LLMUsageIR) -> bool:
    return (usage.final and usage.cache_partition_reported and usage.has("output_tokens")
            and not usage.usage_anomaly)


@dataclass
class KeepaliveAnchor:
    epoch: str
    endpoint_id: str
    binding: str
    scope_key: str
    plan_sequence: int
    turn_id: str
    encoded: EncodedRequest
    profile: KeepaliveProfile
    target: int
    uncached_tail: int
    expires_at: float
    provider: str = ""
    attempts: int = 0
    renewals: int = 0
    spent: float = 0.
    last_cost: float = 0.
    stopped: str = ""
    in_flight: bool = False
    compact_claimed: bool = False

    @property
    def budget(self) -> int:
        return action_budget(self.profile)

    @property
    def next_cost(self) -> float:
        return max(self.last_cost, self.target * self.profile.read_multiplier + self.uncached_tail + 64
                   + self.profile.output_limit * self.profile.output_multiplier)

    def decision(self, now: float) -> str:
        if self.stopped or self.compact_claimed or now >= self.expires_at or self.in_flight:
            return "stop"
        if (self.attempts >= self.budget - 1 or
                self.spent + self.next_cost + self.target * self.profile.read_multiplier
                >= self.target * self.profile.write_multiplier):
            return "compact" if self.renewals else "stop"
        return "keepalive"

    def snapshot(self, now: float) -> dict[str, Any]:
        return {"eligible": self.decision(now) != "stop", "anchor_epoch": self.epoch,
                "endpoint_id": self.endpoint_id, "turn_id": self.turn_id,
                "anchor_ttl_seconds": self.profile.ttl_seconds,
                "anchor_remaining_ttl_seconds": max(0., self.expires_at - now),
                "prefix_tokens": self.target, "context_tokens": self.target,
                "attempts": self.attempts, "renewals": self.renewals, "max_actions": self.budget,
                "estimated_cost": self.spent * self.profile.input_price / 1_000_000,
                "decision": self.decision(now), "reason": self.stopped or self.decision(now)}


def freeze_encoded(encoded: EncodedRequest) -> EncodedRequest:
    return replace(encoded, payload=freeze_json_mapping(encoded.payload),
                   extra_body=freeze_json_mapping(encoded.extra_body))


class CacheKeepalive:
    def __init__(self) -> None:
        self.lock = RLock()
        self.anchor: KeepaliveAnchor | None = None

    def capture(self, *, epoch: str, endpoint_id: str, binding: str, scope_key: str,
                turn_id: str, encoded: EncodedRequest, plan_sequence: int = 0,
                profile: KeepaliveProfile, usage: LLMUsageIR, started_at: float,
                provider: str) -> None:
        with self.lock:
            self.anchor = None
            if not complete_usage(usage):
                return
            if usage.input_tokens > 272000 and profile.ttl_seconds == 1800:
                profile = replace(profile, input_price=profile.input_price * 2, output_multiplier=profile.output_multiplier * .75)
            target = usage.cached_input_tokens + usage.cache_write_input_tokens
            if target <= 0:
                return
            self.anchor = KeepaliveAnchor(epoch, endpoint_id, binding, scope_key, plan_sequence, turn_id,
                freeze_encoded(encoded), profile, target,
                usage.uncached_input_tokens, started_at + profile.ttl_seconds, provider)

    def clear(self) -> None:
        with self.lock:
            self.anchor = None

    def snapshot(self, binding: str) -> dict[str, Any]:
        with self.lock:
            a = self.anchor
            return a.snapshot(time.monotonic()) if a and a.binding == binding else {
                "eligible": False, "reason": "no_matching_keepalive_anchor"}

    def begin(self, epoch: str, binding: str) -> KeepaliveAnchor | None:
        with self.lock:
            a = self.anchor
            if not a or a.epoch != epoch or a.binding != binding or a.decision(time.monotonic()) != "keepalive":
                return None
            a.in_flight = True
            a.attempts += 1
            return a

    def settle(self, a: KeepaliveAnchor, usage: LLMUsageIR, *, started_at: float,
               accepted: bool, provider: str) -> bool:
        with self.lock:
            a.in_flight = False
            previous_spend = a.spent
            if usage.cost_reported and a.profile.input_price > 0:
                a.spent += usage.cost * 1_000_000 / a.profile.input_price
            elif complete_usage(usage):
                a.spent += (usage.cached_input_tokens * a.profile.read_multiplier
                            + usage.cache_write_input_tokens * a.profile.write_multiplier
                            + usage.uncached_input_tokens + usage.output_tokens * a.profile.output_multiplier)
            else:
                a.spent += a.next_cost
            a.last_cost = a.spent - previous_spend
            if self.anchor is not a:
                return False
            if (not accepted or not complete_usage(usage) or usage.cache_write_input_tokens
                    or usage.cached_input_tokens < a.target or usage.output_tokens > a.profile.output_limit
                    or (a.provider and provider != a.provider)
                    or started_at + a.profile.ttl_seconds <= time.monotonic()):
                a.stopped = "renewal_not_confirmed"
                return False
            a.renewals += 1
            a.expires_at = started_at + a.profile.ttl_seconds
            return True

    def claim_compact(self, epoch: str, binding: str) -> bool:
        with self.lock:
            a = self.anchor
            if not a or a.epoch != epoch or a.binding != binding or a.decision(time.monotonic()) != "compact":
                return False
            a.compact_claimed = True
            return True

    def compact_valid(self, epoch: str, binding: str) -> bool:
        with self.lock:
            a = self.anchor
            return bool(a and a.epoch == epoch and a.binding == binding and a.compact_claimed
                        and a.renewals and not a.stopped and time.monotonic() < a.expires_at)

    def prove_compact(self, epoch: str, binding: str, encoded: EncodedRequest) -> bool:
        """Prove unchanged rendered prefix at a matching original lookup boundary."""
        with self.lock:
            if not self.compact_valid(epoch, binding):
                return False
            a = self.anchor
            assert a is not None
            from pal.llm import cache_wire as wire
            from pal.llm.shapes.base import _provider_prefix
            old = {**thaw_json(a.encoded.payload), **thaw_json(a.encoded.extra_body)}
            new = {**thaw_json(encoded.payload), **thaw_json(encoded.extra_body)}
            if any(old.get(k) != new.get(k) for k in ("prompt_cache_key", "session_id", "prompt_cache_options")):
                return False
            markers = [(p, n) for p, n in wire.protocol_nodes(old)
                       if p and ("prompt_cache_breakpoint" in n or "cache_control" in n)]
            if not markers:
                return False
            order = {"tools": 0, "system": 1, "messages": 2, "input": 2}
            path, marker = max(markers, key=lambda item: (order.get(str(item[0][0]), -1),
                tuple(x if isinstance(x, int) else -1 for x in item[0][1:])))
            node = wire.at(new, path)
            if not isinstance(node, dict) or any(node.get(k) != marker.get(k) for k in ("prompt_cache_breakpoint", "cache_control")):
                return False
            # Compare the last marked prefix after removing cache annotations,
            # which are not model content. The input/output budget is excluded.
            left = wire.clean_request(a.encoded, finalize=False)
            right = wire.clean_request(encoded, finalize=False)
            old_prefix = _provider_prefix(thaw_json(left.payload), path, instructions_are_suffix=left.instructions_are_suffix)
            return old_prefix is not None and old_prefix == _provider_prefix(
                thaw_json(right.payload), path, instructions_are_suffix=right.instructions_are_suffix)

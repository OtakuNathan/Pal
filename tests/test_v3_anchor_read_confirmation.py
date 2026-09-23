"""READ-evidence anchor confirmation (dffb210 boundary, v3 warm split).

Submitted marker bytes alone must never authorize a cache-dependent
compaction request.  An anchor-writing request is retained per logical
scope and becomes CONFIRMED solely when the provider reports
serving that anchor prefix from cache on a LATER successful request:
``cached_input_tokens >= prefix_tokens`` with a strictly later plan
sequence than the write.
"""
from __future__ import annotations

import unittest
from dataclasses import replace

from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    LLMUsageIR,
    MessageRole,
    PromptRegionIR,
    TextPartIR,
    WireShape,
)
from pal.llm.prompt_cache import PromptCacheCoordinator
from pal.llm.shapes.base import ShapeContext
from tests.test_prompt_cache_policy import _explicit_context


def _resident_request() -> LLMRequestIR:
    return LLMRequestIR(
        logical_scope_id="pal:resident",
        messages=(
            LLMMessageIR(
                MessageRole.SYSTEM,
                (TextPartIR("stable " * 900),),
                prompt_region=PromptRegionIR.STABLE_SYSTEM,
            ),
            LLMMessageIR(
                MessageRole.USER,
                (TextPartIR("history " * 900),),
                prompt_region=PromptRegionIR.SETTLED_HISTORY,
            ),
            LLMMessageIR(
                MessageRole.USER,
                (TextPartIR("current " * 900),),
                prompt_region=PromptRegionIR.ACTIVE_INPUT,
            ),
        ),
        policy=GenerationPolicyIR(max_output_tokens=1024),
        tools=(),
    )


def _anthropic_context() -> ShapeContext:
    return _explicit_context(
        wire_shape=WireShape.ANTHROPIC_MESSAGES,
        endpoint_id="anthropic",
        model_id="claude-sonnet",
        provider_id="Anthropic",
    )


def _record(coordinator, plan, usage):
    coordinator.record_success(
        plan,
        usage,
        applied_cache_breakpoint_message_ids=tuple(
            item.message_id for item in plan.breakpoints
        ),
    )


class AnchorReadConfirmationTests(unittest.TestCase):
    def test_write_then_read_confirms_the_anchor_request(self):
        context = _anthropic_context()
        coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
        request = _resident_request()

        for _ in range(2):
            _record(coordinator, coordinator.plan(request, context),
                    LLMUsageIR(reported=True))
        anchor_write = coordinator.plan(request, context)
        self.assertTrue(anchor_write.anchor.submitted_message_id,
                        "fixture: the third plan must submit the anchor")
        _record(coordinator, anchor_write, LLMUsageIR(reported=True))

        # Write happened, no read evidence yet → nothing is confirmed.
        self.assertEqual(
            coordinator.confirmed_anchor_request(
                logical_scope_id="pal:resident"),
            {},
            "submitted markers alone must not authorize anything",
        )

        # A LATER request reads the anchor prefix from the provider cache.
        prefix_tokens = coordinator._anchor_evidence[
            anchor_write.scope_key
        ].prefix_tokens
        self.assertGreater(prefix_tokens, 0)
        read_plan = coordinator.plan(request, context)
        _record(
            coordinator,
            read_plan,
            LLMUsageIR(
                reported=True,
                reported_fields=("cached_input_tokens",),
                cached_input_tokens=prefix_tokens,
            ),
        )
        confirmed = coordinator.confirmed_anchor_request(
            logical_scope_id="pal:resident")
        self.assertTrue(confirmed, "read evidence must confirm the anchor")
        self.assertEqual(
            confirmed["anchor_message_id"],
            anchor_write.anchor.submitted_message_id)
        self.assertEqual(confirmed["dialect"], "anthropic_explicit")
        self.assertEqual(confirmed["wire_shape"], "anthropic_messages")
        self.assertIs(confirmed["request"], request)

    def test_short_read_does_not_confirm(self):
        context = _anthropic_context()
        coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
        request = _resident_request()
        for _ in range(2):
            _record(coordinator, coordinator.plan(request, context),
                    LLMUsageIR(reported=True))
        anchor_write = coordinator.plan(request, context)
        _record(coordinator, anchor_write, LLMUsageIR(reported=True))
        evidence = coordinator._anchor_evidence[anchor_write.scope_key]

        read_plan = coordinator.plan(request, context)
        _record(
            coordinator,
            read_plan,
            LLMUsageIR(
                reported=True,
                reported_fields=("cached_input_tokens",),
                cached_input_tokens=max(0, evidence.prefix_tokens - 1),
            ),
        )
        self.assertEqual(
            coordinator.confirmed_anchor_request(
                logical_scope_id="pal:resident"),
            {},
            "a read shorter than the anchor prefix is not proof the ANCHOR "
            "bytes were served from cache",
        )

    def test_endpoint_mismatch_returns_nothing(self):
        context = _anthropic_context()
        coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
        request = _resident_request()
        for _ in range(2):
            _record(coordinator, coordinator.plan(request, context),
                    LLMUsageIR(reported=True))
        anchor_write = coordinator.plan(request, context)
        _record(coordinator, anchor_write, LLMUsageIR(reported=True))
        evidence = coordinator._anchor_evidence[anchor_write.scope_key]
        read_plan = coordinator.plan(request, context)
        _record(
            coordinator,
            read_plan,
            LLMUsageIR(
                reported=True,
                reported_fields=("cached_input_tokens",),
                cached_input_tokens=evidence.prefix_tokens,
            ),
        )
        self.assertTrue(
            coordinator.confirmed_anchor_request(
                logical_scope_id="pal:resident", endpoint_id="anthropic"))
        self.assertEqual(
            coordinator.confirmed_anchor_request(
                logical_scope_id="pal:resident", endpoint_id="other-endpoint"),
            {},
            "the confirmation is bound to the endpoint that holds the cache",
        )

    def test_bunshin_scope_retains_its_own_request_without_cross_scope_reads(self):
        context = _anthropic_context()
        coordinator = PromptCacheCoordinator(rolling_net_threshold_tokens=0)
        request = replace(_resident_request(), logical_scope_id="bunshin:x")
        for _ in range(2):
            _record(coordinator, coordinator.plan(request, context),
                    LLMUsageIR(reported=True))
        anchor_write = coordinator.plan(request, context)
        _record(coordinator, anchor_write, LLMUsageIR(reported=True))
        evidence = coordinator._anchor_evidence.get(anchor_write.scope_key)
        read_plan = coordinator.plan(request, context)
        _record(
            coordinator,
            read_plan,
            LLMUsageIR(
                reported=True,
                reported_fields=("cached_input_tokens",),
                cached_input_tokens=(evidence.prefix_tokens if evidence
                                     else 10_000),
            ),
        )
        confirmed = coordinator.confirmed_anchor_request(logical_scope_id="bunshin:x")
        self.assertIs(confirmed["request"], request)
        for other_scope in ("pal:resident", "bunshin:y"):
            self.assertEqual(
                coordinator.confirmed_anchor_request(logical_scope_id=other_scope), {},
                "retained prefixes must not cross logical scopes",
            )


if __name__ == "__main__":
    unittest.main()

"""G3: builder-level admission facts (pal-two-segment-v3, A02 subset).

Pins the public-builder contract the v3 wiring relies on: compaction_mode
is a formal constructor parameter (validated, forwarded, no post-build
private writes), and the two-segment mode is a distinct, inspectable
admission choice rather than a hidden runtime flag flip.
"""

from __future__ import annotations

import inspect
import unittest

from pal.core.agent_turn_runtime import AgentTurnRuntime
from pal.core.turn_executor import TurnExecutor


class BuilderModeContractTests(unittest.TestCase):
    def test_mode_is_public_and_validated(self):
        signature = inspect.signature(TurnExecutor.__init__)
        param = signature.parameters.get("compaction_mode")
        self.assertIsNotNone(param, "TurnExecutor must take compaction_mode publicly")
        self.assertEqual(param.default, "full_source")

        build_signature = inspect.signature(AgentTurnRuntime.build)
        self.assertIn(
            "compaction_mode", build_signature.parameters,
            "AgentTurnRuntime.build must forward compaction_mode",
        )

        with self.assertRaises(ValueError):
            TurnExecutor(
                None, None, None,
                call_port_async=None,
                build_canonical_prompt=None,
                debug_log_prompt=lambda *a: None,
                debug_log_outcome=lambda *a: None,
                debug_log_reply=lambda *a: None,
                build_llm_tool_contracts=lambda: [],
                handle_failure_async=None,
                render_failure_feedback_text=lambda v: "",
                should_enter_failure_flow_for_tool_result=lambda v: False,
                compaction_mode="bogus",
            )

    def test_default_mode_is_full_source_for_back_compat(self):
        executor = TurnExecutor(
            None, None, None,
            call_port_async=None,
            build_canonical_prompt=None,
            debug_log_prompt=lambda *a: None,
            debug_log_outcome=lambda *a: None,
            debug_log_reply=lambda *a: None,
            build_llm_tool_contracts=lambda: [],
            handle_failure_async=None,
            render_failure_feedback_text=lambda v: "",
            should_enter_failure_flow_for_tool_result=lambda v: False,
        )
        self.assertEqual(executor._compaction_mode, "full_source")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest

from pal.minion.checkpoint import (
    AgentSessionCheckpointError,
    normalize_agent_session_checkpoint,
)
from pal.minion.v2.semantic_orchestration.orchestrator import (
    _worker_terminal_failure,
)


class MinionCheckpointTests(unittest.TestCase):
    def test_v27_rejects_legacy_l1_instead_of_migrating_it_in_place(self) -> None:
        with self.assertRaisesRegex(
            AgentSessionCheckpointError,
            "unsupported checkpoint schema",
        ):
            normalize_agent_session_checkpoint(
                {
                    "schema_version": "5",
                    "session_id": "session-1",
                    "l1_items": [],
                }
            )

    def test_current_l1_requires_current_schema(self) -> None:
        with self.assertRaisesRegex(
            AgentSessionCheckpointError,
            "unsupported checkpoint schema",
        ):
            normalize_agent_session_checkpoint(
                {
                    "schema_version": "4",
                    "l1_turns": [
                        {
                            "turn_id": "turn-1",
                            "state": "settled",
                            "revision": 1,
                            "metadata": {},
                            "messages": [],
                        }
                    ],
                }
            )

    def test_checkpoint_without_an_l1_truth_source_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            AgentSessionCheckpointError,
            "no L1 truth source",
        ):
            normalize_agent_session_checkpoint(
                {"schema_version": "6", "session_id": "session-1"}
            )

    def test_worker_terminal_error_is_not_hidden_by_empty_stderr(self) -> None:
        error_kind, details, retry_directive = _worker_terminal_failure(
            [
                {
                    "event_kind": "terminal",
                    "payload": {
                        "status": "failed",
                        "error_kind": "invalid_agent_session_checkpoint",
                        "error": "continuation does not contain L1",
                        "retry_directive": "do_not_retry",
                    },
                }
            ]
        )

        self.assertEqual(error_kind, "invalid_agent_session_checkpoint")
        self.assertEqual(details, "continuation does not contain L1")
        self.assertEqual(retry_directive, "do_not_retry")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pal.bunshin.checkpoint import (
    AgentSessionCheckpointError,
    LogicalCoroutineCheckpointStore,
    normalize_agent_session_checkpoint,
    open_agent_session_checkpoint,
    seal_agent_session_checkpoint,
)
from pal.bunshin.v2.semantic_orchestration.orchestrator import (
    _worker_terminal_failure,
)


def _private_checkpoint(*, sequence: int = 1, fencing_token: int = 1):
    identity = {
        "logical_coroutine_id": "session-1",
        "workflow_id": "workflow-1",
        "stage_key": "module:module-a:implementation",
        "sequence": sequence,
        "producer_fencing_token": fencing_token,
        "runtime_spec_hash": "spec-1",
    }
    return {
        **identity,
        "coroutine_state": {
            "llm_round_count": 2,
            "tool_call_count": 3,
            "initial_instruction": "secret task",
        },
        "runtime_snapshot": {
            "schema_version": "1",
            **identity,
            "modules": {
                "memory": {
                    "schema_version": "1",
                    "payload": {"secret": "do-not-store-in-plaintext"},
                }
            },
        },
    }


class BunshinCheckpointTests(unittest.TestCase):
    def test_current_schema_rejects_legacy_checkpoint_instead_of_migrating_it(self) -> None:
        with self.assertRaisesRegex(
            AgentSessionCheckpointError,
            "unsupported checkpoint schema",
        ):
            normalize_agent_session_checkpoint(
                {
                    "schema_version": "7",
                    "session_id": "session-1",
                    "l1_items": [],
                }
            )

    def test_encrypted_checkpoint_hides_state_and_rejects_tampering(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal_checkpoint_tamper_"))
        envelope = seal_agent_session_checkpoint(root, _private_checkpoint())
        self.assertNotIn("do-not-store-in-plaintext", repr(envelope))
        self.assertEqual(
            open_agent_session_checkpoint(root, envelope)["coroutine_state"][
                "initial_instruction"
            ],
            "secret task",
        )
        key_path = root / "data" / "security" / "runtime_state.key"
        self.assertEqual(key_path.stat().st_mode & 0o777, 0o600)
        tampered = {
            **envelope,
            "ciphertext": envelope["ciphertext"][:-2] + "aa",
        }
        with self.assertRaisesRegex(
            AgentSessionCheckpointError,
            "cannot be authenticated",
        ):
            open_agent_session_checkpoint(root, tampered)

    def test_store_accepts_only_newer_current_fence_and_deletes_on_terminal(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal_checkpoint_store_"))
        store = LogicalCoroutineCheckpointStore(root)
        first = seal_agent_session_checkpoint(
            root,
            _private_checkpoint(sequence=1, fencing_token=3),
        )
        path = store.publish(
            first,
            expected_logical_coroutine_id="session-1",
            current_fencing_token=3,
        )
        self.assertTrue(path.is_file())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaisesRegex(AgentSessionCheckpointError, "sequence"):
            store.publish(
                first,
                expected_logical_coroutine_id="session-1",
                current_fencing_token=3,
            )
        stale = seal_agent_session_checkpoint(
            root,
            _private_checkpoint(sequence=2, fencing_token=3),
        )
        with self.assertRaisesRegex(AgentSessionCheckpointError, "stale fencing"):
            store.publish(
                stale,
                expected_logical_coroutine_id="session-1",
                current_fencing_token=4,
            )
        resumed = seal_agent_session_checkpoint(
            root,
            _private_checkpoint(sequence=2, fencing_token=4),
        )
        store.publish(
            resumed,
            expected_logical_coroutine_id="session-1",
            current_fencing_token=4,
        )
        self.assertEqual(store.read("session-1")["sequence"], 2)
        self.assertEqual(store.list_logical_coroutine_ids(), ("session-1",))
        changed_stage_payload = _private_checkpoint(sequence=3, fencing_token=5)
        changed_stage_payload["stage_key"] = "module:module-b:implementation"
        changed_stage_payload["runtime_snapshot"]["stage_key"] = (
            "module:module-b:implementation"
        )
        changed_stage = seal_agent_session_checkpoint(root, changed_stage_payload)
        with self.assertRaisesRegex(AgentSessionCheckpointError, "immutable stage_key"):
            store.publish(
                changed_stage,
                expected_logical_coroutine_id="session-1",
                current_fencing_token=5,
            )
        store.delete("session-1")
        self.assertIsNone(store.read("session-1"))
        self.assertEqual(store.list_logical_coroutine_ids(), ())

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

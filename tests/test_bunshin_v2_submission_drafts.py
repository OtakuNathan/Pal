from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.submission_drafts import (
    AUTHORING_CONTRACT_VERSION,
    SubmissionDraftContext,
    SubmissionDraftStore,
    assert_authoring_schema_budget,
)


class SubmissionDraftStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal-v2-draft-"))
        self.repository = BunshinV2Repository(self.root)
        self.repository.ensure_schema()
        self.resource = "node:node_1:writer"
        self.invocation = "inv_worker_1"

    def context(self, *, token: int, fingerprint: str = "input-a") -> SubmissionDraftContext:
        return SubmissionDraftContext(
            workflow_id="wf_1",
            invocation_id=self.invocation,
            lease_resource_key=self.resource,
            fencing_token=token,
            role="verifier",
            mode="module",
            draft_kind="verification",
            input_fingerprint=fingerprint,
        )

    def claim(self) -> int:
        return self.repository.claim_lease(
            self.resource,
            self.invocation,
            ttl_seconds=60,
        ).fencing_token

    def test_mutation_is_idempotent_and_rejects_call_id_reuse(self) -> None:
        context = self.context(token=self.claim())
        store = SubmissionDraftStore(self.root)

        def reducer(payload: dict[str, object]):
            definitions = dict(payload.get("definitions") or {})
            definitions["module"] = "router"
            payload["definitions"] = definitions
            return payload, {"updated": True}

        first = store.mutate(
            context,
            operation_key="call-1",
            request={"name": "router"},
            reducer=reducer,
        )
        repeated = store.mutate(
            context,
            operation_key="call-1",
            request={"name": "router"},
            reducer=lambda _payload: self.fail("idempotent operation reran its reducer"),
        )

        self.assertEqual(first, repeated)
        self.assertEqual(first["draft_version"], 1)
        with self.assertRaisesRegex(ValueError, "reused with different arguments"):
            store.mutate(
                context,
                operation_key="call-1",
                request={"name": "different"},
                reducer=reducer,
            )

    def test_new_fence_inherits_verification_state_for_identical_inputs(self) -> None:
        first_token = self.claim()
        first = self.context(token=first_token)
        store = SubmissionDraftStore(self.root)

        def reducer(payload: dict[str, object]):
            payload.update(
                {
                    "definitions": {"module": "router"},
                    "evidence": {"cases": {"old": {"status": "PASS"}}},
                    "findings": [{"summary": "old finding"}],
                    "summary": {"reviewer_summary": "old summary"},
                }
            )
            return payload, {"updated": True}

        store.mutate(first, operation_key="call-1", request={}, reducer=reducer)
        self.repository.release_lease(self.resource, self.invocation, first_token)
        second_token = self.claim()

        inherited = store.read(self.context(token=second_token))
        self.assertEqual(inherited.payload["definitions"], {"module": "router"})
        self.assertEqual(
            inherited.payload["evidence"],
            {"cases": {"old": {"status": "PASS"}}},
        )
        self.assertEqual(inherited.payload["findings"], [{"summary": "old finding"}])
        self.assertEqual(
            inherited.payload["summary"],
            {"reviewer_summary": "old summary"},
        )
        self.assertEqual(inherited.source_draft_key, first.draft_key)

        self.repository.release_lease(self.resource, self.invocation, second_token)
        third_token = self.claim()
        changed = store.read(
            self.context(token=third_token, fingerprint="input-b"),
            seed={"definitions": {"fresh": True}},
        )
        self.assertEqual(changed.payload, {"definitions": {"fresh": True}})
        self.assertEqual(changed.source_draft_key, "")

    def test_replacement_invocation_inherits_identical_verification_input(self) -> None:
        first_token = self.claim()
        first = self.context(token=first_token)
        store = SubmissionDraftStore(self.root)

        store.mutate(
            first,
            operation_key="record-failure",
            request={},
            reducer=lambda payload: (
                {
                    **payload,
                    "evidence": {"cases": {"priority shape": {"status": "FAIL"}}},
                    "findings": [{"case": "priority shape", "summary": "fraction accepted"}],
                },
                {"recorded": True},
            ),
        )
        self.repository.release_lease(self.resource, self.invocation, first_token)
        replacement_resource = "node:node_1:replacement"
        replacement_invocation = "inv_worker_2"
        replacement_lease = self.repository.claim_lease(
            replacement_resource,
            replacement_invocation,
            ttl_seconds=60,
        )
        replacement = SubmissionDraftContext(
            workflow_id="wf_1",
            invocation_id=replacement_invocation,
            lease_resource_key=replacement_resource,
            fencing_token=replacement_lease.fencing_token,
            role="verifier",
            mode="module",
            draft_kind="verification",
            input_fingerprint="input-a",
        )

        inherited = store.read(replacement)

        self.assertEqual(
            inherited.payload["evidence"],
            {"cases": {"priority shape": {"status": "FAIL"}}},
        )
        self.assertEqual(
            inherited.payload["findings"],
            [{"case": "priority shape", "summary": "fraction accepted"}],
        )
        self.assertEqual(inherited.source_draft_key, first.draft_key)

    def test_non_verification_retry_inherits_definitions_but_not_conclusions(self) -> None:
        first_token = self.claim()
        first = SubmissionDraftContext(
            workflow_id="wf_1",
            invocation_id=self.invocation,
            lease_resource_key=self.resource,
            fencing_token=first_token,
            role="implementation",
            mode="produce",
            draft_kind="candidate",
            input_fingerprint="input-a",
        )
        store = SubmissionDraftStore(self.root)
        store.mutate(
            first,
            operation_key="candidate-progress",
            request={},
            reducer=lambda payload: (
                {
                    **payload,
                    "definitions": {"checks": ["unit"]},
                    "evidence": {"checks": {"unit": {"status": "PASS"}}},
                    "findings": [{"summary": "not candidate state"}],
                    "summary": {"ready": True},
                },
                {"recorded": True},
            ),
        )
        self.repository.release_lease(self.resource, self.invocation, first_token)
        second_token = self.claim()
        second = SubmissionDraftContext(
            workflow_id="wf_1",
            invocation_id=self.invocation,
            lease_resource_key=self.resource,
            fencing_token=second_token,
            role="implementation",
            mode="produce",
            draft_kind="candidate",
            input_fingerprint="input-a",
        )

        inherited = store.read(second)

        self.assertEqual(inherited.payload["definitions"], {"checks": ["unit"]})
        self.assertEqual(inherited.payload["evidence"], {})
        self.assertEqual(inherited.payload["findings"], [])
        self.assertEqual(inherited.payload["summary"], {})

    def test_stale_fence_and_submitted_draft_cannot_mutate(self) -> None:
        first_token = self.claim()
        stale = self.context(token=first_token)
        store = SubmissionDraftStore(self.root)
        snapshot = store.read(stale)
        store.mark_submitted(stale, expected_version=snapshot.version)
        with self.assertRaisesRegex(ValueError, "already frozen"):
            store.mutate(
                stale,
                operation_key="late",
                request={},
                reducer=lambda payload: (payload, {}),
            )

        self.repository.release_lease(self.resource, self.invocation, first_token)
        self.claim()
        with self.assertRaisesRegex(ValueError, "stale fencing token"):
            store.read(stale)

    def test_exact_input_submission_receipt_survives_fence_replacement(self) -> None:
        first_token = self.claim()
        first = self.context(token=first_token)
        store = SubmissionDraftStore(self.root)
        snapshot = store.read(first, seed={"evidence": {"cases": {}}})
        submission = {
            "cases": [{"name": "consumer probe"}],
            "internal_context": {
                "draft_key": first.draft_key,
                "invocation_id": first.invocation_id,
                "fencing_token": first.fencing_token,
                "input_fingerprint": first.input_fingerprint,
            },
        }
        artifact_store = ContentAddressedArtifactStore(self.root, self.repository)
        submission_ref = artifact_store.put_json(
            submission,
            artifact_type="VerifierRoleSubmissionArtifact",
        )
        payload_hash = hashlib.sha256(
            json.dumps(
                submission,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        store.mark_submitted(
            first,
            expected_version=snapshot.version,
            submission_artifact_ref=submission_ref.to_dict(),
            submission_payload_hash=payload_hash,
        )

        self.repository.release_lease(self.resource, self.invocation, first_token)
        second_token = self.claim()
        receipt = store.latest_submitted(
            workflow_id=first.workflow_id,
            invocation_id=first.invocation_id,
            role=first.role,
            mode=first.mode,
            draft_kind=first.draft_kind,
            input_fingerprint=first.input_fingerprint,
        )

        self.assertIsNotNone(receipt)
        assert receipt is not None
        self.assertEqual(receipt.fencing_token, first_token)
        self.assertEqual(receipt.submission_artifact_ref, submission_ref.to_dict())
        self.assertEqual(
            artifact_store.read_json(receipt.submission_artifact_ref),
            submission,
        )
        self.assertIsNone(
            store.latest_submitted(
                workflow_id=first.workflow_id,
                invocation_id=first.invocation_id,
                role=first.role,
                mode=first.mode,
                draft_kind=first.draft_kind,
                input_fingerprint="changed-input",
            )
        )
        self.repository.release_lease(self.resource, self.invocation, second_token)

    def test_workspace_binding_requires_explicit_authoring_contract_version(self) -> None:
        binding = {
            "bunshin_v2": {
                "workflow_id": "wf_1",
                "invocation_id": self.invocation,
                "lease_resource_key": self.resource,
                "fencing_token": 1,
                "role": "verifier",
                "mode": "module",
                "authoring_input_fingerprint": "input-a",
            }
        }
        with self.assertRaisesRegex(ValueError, "authoring_contract_version"):
            SubmissionDraftContext.from_workspace(binding, draft_kind="verification")

        binding["bunshin_v2"]["authoring_contract_version"] = AUTHORING_CONTRACT_VERSION
        context = SubmissionDraftContext.from_workspace(
            binding,
            draft_kind="verification",
        )
        self.assertEqual(context.authoring_contract_version, AUTHORING_CONTRACT_VERSION)

    def test_store_rejects_stale_authoring_contract_even_without_workspace_binder(self) -> None:
        token = self.claim()
        stale = SubmissionDraftContext(
            **{
                **self.context(token=token).__dict__,
                "authoring_contract_version": "1",
            }
        )
        with self.assertRaisesRegex(ValueError, "authoring contract is stale"):
            SubmissionDraftStore(self.root).read(stale)


class AuthoringSchemaBudgetTests(unittest.TestCase):
    def test_accepts_small_semantic_schema(self) -> None:
        assert_authoring_schema_budget(
            {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
            owner="semantic_tool",
        )

    def test_rejects_document_compiler_shapes(self) -> None:
        invalid = (
            {"type": "object", "properties": {f"field_{index}": {"type": "string"} for index in range(13)}},
            {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object"}}}},
            {"type": "object", "properties": {"choice": {"oneOf": [{"type": "string"}]}}},
            {"type": "object", "additionalProperties": {"type": "string"}},
        )
        for index, schema in enumerate(invalid):
            with self.subTest(index=index), self.assertRaises(ValueError):
                assert_authoring_schema_budget(schema, owner=f"invalid_{index}")


if __name__ == "__main__":
    unittest.main()

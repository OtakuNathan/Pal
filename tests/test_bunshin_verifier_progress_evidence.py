"""Completion retries observe durable verifier work, never bookkeeping churn."""
from __future__ import annotations

import asyncio
import copy
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from pal.bunshin.runner import BunshinRunner
from pal.bunshin.runner_components.artifacts import Artifacts
from pal.bunshin.runner_components.completion import Completion
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.semantic_evidence import run_shell_evidence
from pal.bunshin.v2.submission_drafts import (
    AUTHORING_CONTRACT_VERSION,
    SubmissionDraftContext,
    SubmissionDraftStore,
)
from pal.shared import BunshinInvocationPack, RuntimeStatus, ToolExecutionResult
from pal.shared.tool_protocol import new_tool_call


@pytest.fixture
def verifier(tmp_path):
    runtime = tmp_path / "runtime"
    repo = tmp_path / "repo"
    repo.mkdir()
    repository = BunshinV2Repository(runtime)
    lease = repository.leases.claim_lease("progress-verifier", "progress-test", ttl_seconds=300)
    workspace = {
        "runtime_root": str(runtime),
        "repo_path": str(repo),
        "write_path_scopes": [{"kind": "directory", "path": "tests/verifier"}],
        "output_policy": {"primary_artifact": "verification_submission.json"},
        "bunshin_v2": {
            "workflow_id": "workflow-test", "invocation_id": "progress-test",
            "lease_resource_key": lease.resource_key, "fencing_token": lease.fencing_token,
            "role": "verifier", "mode": "module",
            "authoring_input_fingerprint": "candidate-current",
            "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
        },
    }
    pack = BunshinInvocationPack(invocation_id="progress-test", workspace=workspace)
    return Completion(Artifacts(pack), pack, runtime)


class ShellAdapter:
    calls = 0

    async def execute_tool_async(self, call, **_kwargs):
        self.calls += 1
        result = subprocess.run(
            call.args["cmd"], shell=True, cwd=call.args.get("cwd"),
            text=True, capture_output=True, check=False, timeout=10,
        )
        return ToolExecutionResult(
            name=call.name, call_id=call.call_id, ok=result.returncode == 0,
            status=RuntimeStatus.OK if result.returncode == 0 else RuntimeStatus.INVALID,
            text=result.stdout or f"exit {result.returncode}",
            llm_text=result.stdout or f"exit {result.returncode}",
            structured={"returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr},
        )


def record_case(verifier, adapter, *, call_id="case-1", code="print('verified')"):
    return asyncio.run(run_shell_evidence(
        new_tool_call(
            name="op_bunshin_verification_run_focused_test", call_id=call_id,
            args={
                "name": "durable-check",
                "command": f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
                "path": "src/router.py",
            },
        ),
        workspace=verifier.pack.workspace, original_adapter=adapter,
        draft_kind="verification", case_kind="unit", obligation_tag="focused_tests",
    ))


def draft_context(verifier):
    return SubmissionDraftContext.from_workspace(verifier.pack.workspace, draft_kind="verification")


def mutate_draft(verifier, key, update):
    def reducer(payload):
        update(payload)
        return payload, {"updated": True}

    return SubmissionDraftStore(verifier.runtime_root).mutate(
        draft_context(verifier), operation_key=key, request={"mutation": key}, reducer=reducer,
    )


def test_real_semantic_case_changes_progress_and_unchanged_replay_does_not(verifier):
    before = verifier.completion_gate_progress_marker()
    adapter = ShellAdapter()
    first = record_case(verifier, adapter)
    assert first.ok, first.llm_text
    snapshot = SubmissionDraftStore(verifier.runtime_root).read(draft_context(verifier))
    assert snapshot.payload["evidence"]["cases"]["durable-check"]["status"] == "PASS"
    recorded = verifier.completion_gate_progress_marker()
    assert recorded != before

    replay = record_case(verifier, adapter, call_id="same-case-new-call")
    assert replay.ok and replay.structured["reused"]
    assert adapter.calls == 1
    assert verifier.completion_gate_progress_marker() == recorded
    # Progress extends recovery, but never substitutes for successful submission.
    assert not verifier.completion_evidence_present()
    assert not verifier.required_primary_artifact_present()


def test_real_changed_semantic_result_counts_without_corpus_changes(verifier):
    adapter = ShellAdapter()
    first = record_case(verifier, adapter)
    assert first.ok, first.llm_text
    before = verifier.completion_gate_progress_marker()
    failed = record_case(verifier, adapter, call_id="changed-case", code="raise SystemExit(1)")
    assert failed.structured.get("case", {}).get("status") == "FAIL", failed.llm_text
    assert adapter.calls == 2
    assert verifier.completion_gate_progress_marker() != before


def test_versions_timestamps_duplicate_metadata_and_unrelated_fingerprints_do_not_count(verifier):
    assert record_case(verifier, ShellAdapter()).ok
    before = verifier.completion_gate_progress_marker()
    store = SubmissionDraftStore(verifier.runtime_root)
    version = store.read(draft_context(verifier)).version

    def metadata_only(payload):
        payload["updated_at"] = "2099-01-01"
        payload["summary"] = {"request_count": 999}
        payload["evidence"]["operation_count"] = 999
        case = payload["evidence"]["cases"]["durable-check"]
        case.update(recorded_sequence=999, request_fingerprint="unrelated-repo-change",
                    call_id="new-call", updated_at="2099-01-01", metadata={"count": 999},
                    description="Reworded prose only", summary="Reworded summary only")
        case["environment"]["timestamp"] = "2099-01-01"
        case["stdout_ref"].update(byte_size=999, provenance={"call_id": "new-call"})
        case["locations"][0]["metadata"] = {"count": 999}
        case["obligation_tags"] *= 2
        case["locations"] *= 2
        payload["evidence"]["cases"]["duplicate-storage-key"] = copy.deepcopy(case)

    mutate_draft(verifier, "bookkeeping", metadata_only)
    assert store.read(draft_context(verifier)).version > version
    assert verifier.completion_gate_progress_marker() == before


@pytest.mark.parametrize(("field", "value"), [
    ("status", "UNKNOWN"),
    ("exit_code", 7),
    ("stdout_ref", {"sha256": "a" * 64}),
    ("stderr_ref", {"sha256": "b" * 64}),
    ("input_fingerprint", "candidate-new"),
])
def test_durable_case_result_and_content_identity_are_observed(verifier, field, value):
    assert record_case(verifier, ShellAdapter()).ok
    before = verifier.completion_gate_progress_marker()
    mutate_draft(verifier, "changed-evidence", lambda payload: payload["evidence"]["cases"][
        "durable-check"
    ].update({field: value, "execution_id": "new-execution"}))
    assert verifier.completion_gate_progress_marker() != before


def test_explicit_corpus_create_edit_delete_count_but_touch_and_other_files_do_not(verifier):
    root = Path(verifier.pack.workspace["repo_path"])
    corpus = root / "tests/verifier"
    corpus.mkdir(parents=True)
    before = verifier.completion_gate_progress_marker()
    check = corpus / "check.py"
    check.write_text("assert True\n")
    created = verifier.completion_gate_progress_marker()
    assert created != before
    check.touch()
    (root / "unrelated.txt").write_text("unrelated repository content")
    (root / "tests/outside.py").write_text("assert False\n")
    assert verifier.completion_gate_progress_marker() == created
    check.write_text("assert 1 + 1 == 2\n")
    assert verifier.completion_gate_progress_marker() != created
    check.unlink()
    assert verifier.completion_gate_progress_marker() == before


def test_explicit_file_scope_does_not_observe_sibling_files(verifier):
    root = Path(verifier.pack.workspace["repo_path"])
    verifier.pack.workspace["write_path_scopes"] = [{"kind": "file", "path": "test_one.py"}]
    before = verifier.completion_gate_progress_marker()
    (root / "test_other.py").write_text("assert True\n")
    assert verifier.completion_gate_progress_marker() == before
    (root / "test_one.py").write_text("assert True\n")
    assert verifier.completion_gate_progress_marker() != before


def test_changed_bound_candidate_identity_counts(verifier):
    before = verifier.completion_gate_progress_marker()
    verifier.pack.workspace["bunshin_v2"]["authoring_input_fingerprint"] = "new-candidate"
    assert verifier.completion_gate_progress_marker() != before


def test_changed_git_candidate_counts_without_hashing_unrelated_worktree_content(verifier):
    root = Path(verifier.pack.workspace["repo_path"])

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    git("init")
    git("-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "--allow-empty", "-m", "candidate one")
    before = verifier.completion_gate_progress_marker()
    git("-c", "user.name=Test", "-c", "user.email=test@example.com",
        "commit", "--allow-empty", "-m", "candidate two")
    assert verifier.completion_gate_progress_marker() != before


def test_unbound_corpus_does_not_scan_repository(verifier):
    verifier.pack.workspace.pop("write_path_scopes")
    before = verifier.completion_gate_progress_marker()
    (Path(verifier.pack.workspace["repo_path"]) / "test_unbound.py").write_text("assert True\n")
    assert verifier.completion_gate_progress_marker() == before


@pytest.mark.parametrize("symlink_scope", [False, True])
def test_corpus_does_not_follow_file_or_scope_symlinks(verifier, symlink_scope):
    root = Path(verifier.pack.workspace["repo_path"])
    corpus = root / "tests/verifier"
    outside = root.parent / "outside"
    outside.mkdir()
    target = outside / "check.py"
    target.write_text("assert True\n")
    if symlink_scope:
        corpus.parent.mkdir()
        corpus.symlink_to(outside, target_is_directory=True)
    else:
        corpus.mkdir(parents=True)
        (corpus / "check.py").symlink_to(target)
    before = verifier.completion_gate_progress_marker()
    target.write_text("assert False\n")
    assert verifier.completion_gate_progress_marker() == before


def test_scratch_only_verifier_observes_bound_scratch_without_repo_files(verifier):
    scratch = verifier.runtime_root / "review_scratch"
    scratch.mkdir()
    verifier.pack.workspace.update(verification_scratch_only=True, review_scratch_dir=str(scratch))
    before = verifier.completion_gate_progress_marker()
    (Path(verifier.pack.workspace["repo_path"]) / "unrelated.py").write_text("assert False\n")
    assert verifier.completion_gate_progress_marker() == before
    (scratch / "probe.py").write_text("assert True\n")
    assert verifier.completion_gate_progress_marker() != before


def test_other_roles_do_not_read_verification_drafts_or_corpus(verifier, monkeypatch):
    verifier.pack.workspace["bunshin_v2"].update(role="architect", mode="author")
    read = SubmissionDraftStore.read

    def forbid_verification_read(self, context, **kwargs):
        assert context.draft_kind != "verification"
        return read(self, context, **kwargs)

    monkeypatch.setattr(SubmissionDraftStore, "read", forbid_verification_read)
    before = verifier.completion_gate_progress_marker()
    corpus = Path(verifier.pack.workspace["repo_path"]) / "tests/verifier"
    corpus.mkdir(parents=True)
    (corpus / "check.py").write_text("assert True\n")
    assert verifier.completion_gate_progress_marker() == before


def test_unavailable_verification_draft_propagates_instead_of_claiming_stagnation(verifier, monkeypatch):
    read = SubmissionDraftStore.read

    def unavailable(self, context, **kwargs):
        if context.draft_kind == "verification":
            raise RuntimeError("verification draft unavailable")
        return read(self, context, **kwargs)

    monkeypatch.setattr(SubmissionDraftStore, "read", unavailable)
    with pytest.raises(RuntimeError, match="verification draft unavailable"):
        verifier.completion_gate_progress_marker()


def test_corpus_progress_extends_retry_without_bypassing_missing_submission(verifier):
    corpus = Path(verifier.pack.workspace["repo_path"]) / "tests/verifier"
    corpus.mkdir(parents=True)

    async def noop(*_args, **_kwargs):
        return None

    class RepairingRunner(BunshinRunner):
        calls = 0

        async def run_agent_loop(self, bundle, *, forced_retry_note=""):
            self.calls += 1
            if self.calls <= 2:
                (corpus / "check.py").write_text(f"assert {self.calls} > 0\n")
            return "done"

        def __post_init__(self):
            super().__post_init__()
            self.components.agent_session.run_agent_loop = self.run_agent_loop

    runner = RepairingRunner(
        runtime_root=verifier.runtime_root, pack=verifier.pack,
        bunshin_id="progress-test", run_id="attempt-test", write_event=noop, read_decision=noop,
    )
    asyncio.run(runner.components.invocation.run_v2_invocation(None))
    assert runner.calls == 3
    assert runner.components.status.blocked_kind == "completion_gate_stalled"
    assert not runner.components.completion.completion_evidence_present()

"""Unbound V2 SWE Verifier workflow with pre-seeded verification_report.yaml."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from pal.bunshin.profiles import CAPABILITY_GROUPS
from pal.bunshin.prompt_adapter import render_bunshin_task_prompt
from pal.bunshin.review_receipts import _review_tool_evidence_ref
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
from pal.bunshin.semantic_orchestration.role_policy import apply_role_capability_policy
from pal.bunshin.swe_verification import (
    semantic_verification_submission_errors,
    swe_verification_tool_result,
)
from pal.bunshin.verification_builder import (
    SHELL_EVIDENCE_CAPABILITIES,
    VERIFICATION_EVIDENCE_CAPABILITIES,
    compile_verification_invocation_tool_contract,
)
from pal.bunshin.verification_corpus import verification_corpus_snapshot
from pal.bunshin.verification_report import (
    VERIFICATION_REPORT_FILENAME,
    bind_verification_report_file,
    read_verification_report_yaml,
)
from pal.bunshin.work_items import (
    edit_finding_tool_result,
    render_work_item_context,
    update_checklist_tool_result,
)
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import (
    EffectOutcome,
    EffectReceipt,
    StructuredToolOutput,
    ToolHandlerResult,
)
from pal.execution.tool_semantics import DIRECT_LOCAL_WRITE
from pal.bunshin.scoped_execution import BunshinScopedExecutionShellInput
from pal.shared import BunshinInvocationPack, RuntimeStatus, ToolExecutionResult
from pal.shared.tool_protocol import new_tool_call
from tests.capability_fixture import mount_test_capability


def _valid_report_yaml(
    *,
    module: str = "router",
    candidate_digest: str = "",
    command: str = "pytest tests/router/verifier",
    adversarial_status: str = "pass",
    adversarial_notes: str = "Verified boundary and failure cases.",
) -> str:
    payload = {
        "schema_version": "1",
        "module": module,
        **({"candidate_digest": candidate_digest} if candidate_digest else {}),
        "verdict_summary": "Replayed tests and checked current Candidate delta.",
        "focus_areas": {
            "regressions": {
                "status": "pass",
                "commands": [command],
                "notes": "All regression cases passed.",
            },
            "candidate_delta": {
                "status": "pass",
                "commands": [command],
                "notes": "Inspected changed router lines.",
            },
            "adversarial_checks": {
                "status": adversarial_status,
                "commands": [] if adversarial_status == "not_applicable" else [command],
                "notes": adversarial_notes,
            },
        },
        "extra_checks": [],
    }
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)


def _setup_unbound_verifier(tmp_path: Path, *, with_test_file: bool = True, scratch_only: bool = False, verdict_scope: str = ""):
    from tests.test_bunshin_verification import BunshinVerificationTests

    fixture = BunshinVerificationTests()
    fixture.setUp()
    repo, digest = fixture._git_repo("unbound-verifier")
    if not with_test_file:
        for existing in list((repo / "tests" / "router" / "verifier").glob("*")):
            if existing.is_file():
                existing.unlink()
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "empty verifier corpus"], check=True)
        digest = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    scratch = tmp_path / "review-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    view = {
        "module_name": "router",
        "verification_corpus": {"kind": "directory", "path": "tests/router/verifier"},
    }
    contract = compile_verification_invocation_tool_contract(work_view=view, verification_policy={})
    workspace = fixture._bind_workspace(
        {
            "repo_path": str(repo),
            "review_scratch_dir": str(scratch),
            "artifact_dir": str(tmp_path / "artifacts"),
            "artifact_stage_dir": str(tmp_path / "stage"),
            "write_path_scopes": [view["verification_corpus"]],
            "verification_scratch_only": scratch_only,
            **({"verification_verdict_scope": verdict_scope, "verification_candidate_digest": digest,
                "repair_suite_required": not scratch_only} if verdict_scope else {}),
            "review_tool_evidence_refs": [],
        },
        role="verifier",
    )
    workspace["bunshin"]["verification_tool_contract"] = contract
    workspace["invocation_id"] = workspace["bunshin"]["invocation_id"]
    if not verdict_scope:
        workspace = bind_verification_report_file(
            workspace, work_view=view, verification_policy=contract["verification_policy"], candidate_digest=digest,
        )
    pack = apply_role_capability_policy(
        BunshinInvocationPack(
            invocation_id=workspace["invocation_id"],
            workspace=workspace,
            allowed_capabilities=list(CAPABILITY_GROUPS["swe_verifier_workspace"])
            + list(CAPABILITY_GROUPS["v2_swe_verification"]),
        ),
        activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
    )
    base_runtime = ExecutionRuntime()

    def _run_shell(args):
        completed = subprocess.run(
            args.cmd,
            shell=True,
            cwd=str(repo),
            capture_output=True,
            text=True,
            check=False,
        )
        output = {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        return ToolHandlerResult(
            output=output,
            llm_text=completed.stdout or completed.stderr or "ok",
            effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED, receipt={"returncode": completed.returncode}),
        )

    mount_test_capability(
        base_runtime,
        canonical_path="op_exec_shell",
        alias="run_shell",
        InputModel=BunshinScopedExecutionShellInput,
        OutputModel=StructuredToolOutput,
        handler=_run_shell,
        execution=DIRECT_LOCAL_WRITE,
        examples=({"cmd": "true"},),
        metadata={"preserve_role_contract": True},
    )
    runtime = BunshinScopedExecutionRuntime(base_runtime, list(pack.allowed_capabilities), workspace=workspace)
    return fixture, workspace, pack, runtime, base_runtime, repo, digest, view


def test_unbound_verifier_omits_legacy_evidence_wrappers_and_keeps_shell_compat(tmp_path):
    assert SHELL_EVIDENCE_CAPABILITIES  # Required by pal-shell-native hook integration.
    fixture, workspace, pack, runtime, base_runtime, _repo, _digest, _view = _setup_unbound_verifier(tmp_path)
    try:
        for capability in VERIFICATION_EVIDENCE_CAPABILITIES:
            assert capability not in CAPABILITY_GROUPS["v2_swe_verification"]
            assert capability not in pack.allowed_capabilities
            assert capability not in runtime.allowed_capabilities
        for required in (
            "op_file_read",
            "op_file_edit",
            "op_file_write",
            "op_exec_shell",
            "op_bunshin_update_finding",
            "op_bunshin_remove_finding",
            "op_bunshin_verification_pass",
            "op_bunshin_verification_request_module_repair",
            "op_bunshin_update_checklist",
        ):
            assert required in pack.allowed_capabilities
        rendered_prompt = render_bunshin_task_prompt(pack)
        assert "## Writable Outputs" in rendered_prompt
        assert "tests/router/verifier/verification_report.yaml" in rendered_prompt
    finally:
        runtime._execution.runtime.shutdown()
        base_runtime.shutdown()
        fixture.tearDown()


def test_preseeded_report_in_worktree_refreshes_on_new_candidate_but_preserves_same_candidate_retry(tmp_path):
    fixture, workspace, _pack, runtime, base_runtime, repo, digest, view = _setup_unbound_verifier(tmp_path)
    try:
        report_path = repo / "tests" / "router" / "verifier" / VERIFICATION_REPORT_FILENAME
        assert report_path.is_file()
        assert workspace["verification_report_relpath"] == "tests/router/verifier/verification_report.yaml"
        initial = read_verification_report_yaml(report_path)
        assert initial["candidate_digest"] == digest
        assert initial["verdict_summary"] == ""

        # Editing the report on the same Candidate is preserved on retry.
        report_path.write_text(_valid_report_yaml(candidate_digest=digest), encoding="utf-8")
        same_retry = bind_verification_report_file(
            workspace, work_view=view, candidate_digest=digest,
        )
        assert read_verification_report_yaml(Path(same_retry["verification_report_path"]))["verdict_summary"] != ""

        # A report without identity cannot settle the current or a future Candidate.
        from pal.bunshin.verification_report import load_and_validate_verification_report
        report_path.write_text(_valid_report_yaml(), encoding="utf-8")
        assert any("candidate_digest" in error for error in load_and_validate_verification_report(
            workspace, outcome="pass",
        )[-1])
        # A new Candidate digest refreshes the template so old verdicts never carry over.
        new_cycle = bind_verification_report_file(
            workspace, work_view=view, candidate_digest="new-candidate-sha-9999",
        )
        refreshed = read_verification_report_yaml(Path(new_cycle["verification_report_path"]))
        assert refreshed["candidate_digest"] == "new-candidate-sha-9999"
        assert refreshed["verdict_summary"] == ""
    finally:
        runtime._execution.runtime.shutdown()
        base_runtime.shutdown()
        fixture.tearDown()


def test_writing_report_after_final_shell_run_keeps_receipts_fresh_while_test_edit_invalidates(tmp_path):
    fixture, workspace, _pack, runtime, base_runtime, repo, digest, view = _setup_unbound_verifier(tmp_path)
    try:
        report_path = Path(workspace["verification_report_path"])
        probe_path = repo / "tests" / "router" / "verifier" / "test_probe.py"
        probe_path.write_text("assert 1 + 1 == 2\n", encoding="utf-8")

        # 1. Run ordinary shell test directly through scoped runtime.
        run_res = asyncio.run(
            runtime.execute_tool_async(
                new_tool_call(
                    name="run_shell",
                    args={"cmd": f"{sys.executable} tests/router/verifier/test_probe.py"},
                    call_id="shell-1",
                )
            )
        )
        assert run_res.ok, run_res.llm_text
        snapshot_before_report = verification_corpus_snapshot(workspace)

        # 2. Untouched report template is rejected before PASS.
        untouched = swe_verification_tool_result(
            new_tool_call(name="op_bunshin_verification_pass", args={}, call_id="pass-untouched"),
            workspace,
            [],
        )
        assert not untouched.ok
        assert "verdict_summary" in untouched.llm_text

        # 3. Filling verification_report.yaml AFTER run_shell keeps corpus snapshot
        #    and fresh receipt intact (and records report_write, not test_write).
        report_path.write_text(
            _valid_report_yaml(
                candidate_digest=digest,
                command="shell-1",
                adversarial_status="not_applicable",
                adversarial_notes="No concurrency or FFI surface in this pure module.",
            ),
            encoding="utf-8",
        )
        write_receipt = _review_tool_evidence_ref(
            "op_file_write",
            new_tool_call(
                name="op_file_write",
                args={"file_path": "tests/router/verifier/verification_report.yaml"},
                call_id="write-report",
            ),
            ToolExecutionResult(name="op_file_write", ok=True, text="ok", llm_text="ok", status=RuntimeStatus.OK),
            workspace,
        )
        assert write_receipt["kind"] == "report_write"
        workspace["review_tool_evidence_refs"].append(write_receipt)
        assert verification_corpus_snapshot(workspace) == snapshot_before_report

        # 4. PASS succeeds cleanly with ONLY ordinary run_shell + verification_report.yaml!
        passed = swe_verification_tool_result(
            new_tool_call(name="op_bunshin_verification_pass", args={}, call_id="pass-ok"),
            workspace,
            [],
        )
        assert passed.ok, passed.llm_text
        submission = json.loads((tmp_path / "stage" / "verification_submission.json").read_text(encoding="utf-8"))
        assert submission["outcome"] == "pass"
        assert submission["recorded_results"] == []
        assert submission["verification_report_path"] == "tests/router/verifier/verification_report.yaml"
        assert submission["verification_report"]["focus_areas"]["adversarial_checks"]["status"] == "not_applicable"

        # Settlement must attach precisely what the receipt accepted, even if the file changes.
        from pal.bunshin.semantic_orchestration.verification_settlement import _verification_report_artifact_fields

        accepted_bytes = report_path.read_bytes()
        report_path.write_text("# edited after acceptance", encoding="utf-8")
        store = fixture.store
        fields, _ = _verification_report_artifact_fields(store, submission)
        assert store.read_bytes(fields["verification_report_ref"]) == accepted_bytes

        # 5. Editing an actual test file after run_shell STILL invalidates freshness!
        probe_path.write_text("assert 2 + 2 == 4\n", encoding="utf-8")
        stale_errors = semantic_verification_submission_errors(
            submission,
            work_view=view,
            changed_paths=[
                "tests/router/verifier/test_probe.py",
                "tests/router/verifier/verification_report.yaml",
            ],
            current_case_paths=["tests/router/verifier/test_probe.py"],
            corpus_scope=view["verification_corpus"],
            scratch_only=False,
            workspace=workspace,
        )
        assert any("current corpus" in err or "fresh" in err for err in stale_errors)
    finally:
        runtime._execution.runtime.shutdown()
        base_runtime.shutdown()
        fixture.tearDown()


def test_report_validation_rejects_empty_corpus_duplicate_keys_and_fail_on_pass(tmp_path):
    fixture, workspace, _pack, runtime, base_runtime, _repo, digest, _view = _setup_unbound_verifier(
        tmp_path, with_test_file=False,
    )
    try:
        report_path = Path(workspace["verification_report_path"])
        run_res = asyncio.run(
            runtime.execute_tool_async(
                new_tool_call(
                    name="run_shell",
                    args={"cmd": f"{sys.executable} -c 'print(1)'"},
                    call_id="s1",
                )
            )
        )
        assert run_res.ok, run_res.llm_text

        # Pre-seeded verification_report.yaml alone does NOT count as a durable test file.
        report_path.write_text(_valid_report_yaml(candidate_digest=digest), encoding="utf-8")
        empty_corpus = swe_verification_tool_result(
            new_tool_call(name="op_bunshin_verification_pass", args={}, call_id="p-empty"),
            workspace,
            [],
        )
        assert not empty_corpus.ok
        assert "durable verifier-authored case" in empty_corpus.llm_text

        # Duplicate YAML keys are rejected.
        report_path.write_text(
            'verdict_summary: "first"\nverdict_summary: "duplicate"\n',
            encoding="utf-8",
        )
        dup_res = swe_verification_tool_result(
            new_tool_call(name="op_bunshin_verification_pass", args={}, call_id="p-dup"),
            workspace,
            [],
        )
        assert not dup_res.ok
        assert "duplicate key" in dup_res.llm_text

        # status=not_applicable without notes is rejected.
        report_path.write_text(
            _valid_report_yaml(candidate_digest=digest, adversarial_status="not_applicable", adversarial_notes=""),
            encoding="utf-8",
        )
        missing_notes = swe_verification_tool_result(
            new_tool_call(name="op_bunshin_verification_pass", args={}, call_id="p-notes"),
            workspace,
            [],
        )
        assert not missing_notes.ok
        assert "requires non-empty notes" in missing_notes.llm_text

        # status=fail cannot submit PASS.
        report_path.write_text(
            _valid_report_yaml(candidate_digest=digest, adversarial_status="fail", adversarial_notes="Found bug."),
            encoding="utf-8",
        )
        fail_on_pass = swe_verification_tool_result(
            new_tool_call(name="op_bunshin_verification_pass", args={}, call_id="p-fail"),
            workspace,
            [],
        )
        assert not fail_on_pass.ok
        assert "PASS cannot be submitted" in fail_on_pass.llm_text
    finally:
        runtime._execution.runtime.shutdown()
        base_runtime.shutdown()
        fixture.tearDown()


def test_report_binds_required_policy_history_and_sink_evidence(tmp_path):
    from pal.bunshin.verification_readiness import report_evidence_errors

    ws = bind_verification_report_file(
        {"review_scratch_dir": str(tmp_path), "verification_scratch_only": True},
        work_view={"historical_repair_bills": [{"case_name": "retry after timeout"}]},
        verification_policy={"require_lsp": True}, system_delivery_view={"scenarios": {"launch": {}}},
    )
    report = read_verification_report_yaml(Path(ws["verification_report_path"]))
    areas = report["focus_areas"]
    assert {"historical_regression/retry after timeout", "system_delivery/launch", "lsp"} <= areas.keys()
    for entry in areas.values():
        entry.update(status="pass", commands=["run tests"])
    receipts = [{"kind": "command", "tool_name": "op_exec_shell", "args": {"cmd": "run tests"}, "ok": True}]
    assert any("LSP diagnostics" in item for item in report_evidence_errors(report, receipts, ws))
    receipts.append({"kind": "lsp", "tool_name": "op_lsp_diagnostics", "call_id": "diagnostics", "ok": True})
    areas["lsp"]["commands"] = ["diagnostics"]
    assert not report_evidence_errors(report, receipts, ws)
    del areas["system_delivery/launch"]
    assert any("missing required" in item for item in report_evidence_errors(report, receipts, ws))


def test_report_io_rejects_symbolic_links(tmp_path):
    from pal.bunshin.verification_report import load_and_validate_verification_report

    ws = bind_verification_report_file(
        {"repo_path": str(tmp_path), "write_path_scopes": [{"kind": "directory", "path": "cases"}]},
        work_view={}, candidate_digest="candidate",
    )
    path = Path(ws["verification_report_path"])
    path.unlink()
    path.symlink_to("missing")
    with pytest.raises((OSError, ValueError)):
        bind_verification_report_file(ws, work_view={}, candidate_digest="candidate")
    assert load_and_validate_verification_report(ws, outcome="pass")[-1]
    assert not (path.parent / "missing").exists()


def test_scratch_report_binding_rejects_linked_scratch_directory(tmp_path):
    destination = tmp_path / "destination"
    destination.mkdir()
    scratch = tmp_path / "scratch"
    scratch.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError, match="destination root"):
        bind_verification_report_file(
            {"review_scratch_dir": str(scratch), "verification_scratch_only": True},
            work_view={},
        )
    assert not (destination / VERIFICATION_REPORT_FILENAME).exists()


@pytest.mark.parametrize("scratch", [False, True])
def test_nested_same_named_fixture_remains_a_verification_case(tmp_path, scratch):
    from pal.bunshin.swe_verification import _verification_case_files

    scope = {"kind": "directory", "path": "cases"}
    (tmp_path / "scratch").mkdir()
    workspace = bind_verification_report_file(
        {"repo_path": str(tmp_path), "write_path_scopes": [scope],
         "review_scratch_dir": str(tmp_path / "scratch"), "verification_scratch_only": scratch},
        work_view={},
    )
    report = Path(workspace["verification_report_path"])
    fixture = report.parent / "fixtures" / report.name
    fixture.parent.mkdir()
    fixture.write_text("sample: true\n")
    prefix = "review_scratch" if scratch else "cases"
    assert _verification_case_files(workspace) == [f"{prefix}/fixtures/{report.name}"]
    for path, kind in [(report, "report_write"), (fixture, "test_write")]:
        call = new_tool_call(name="op_file_edit", args={"file_path": str(path)})
        result = ToolExecutionResult(name=call.name, ok=True, text="ok", llm_text="ok", status=RuntimeStatus.OK)
        assert _review_tool_evidence_ref(call.name, call, result, workspace)["kind"] == kind


def test_report_yaml_can_roundtrip_through_the_submission_artifact(tmp_path):
    path = tmp_path / "verification_report.yaml"
    path.write_text("checked_on: 2026-11-20\n")
    assert read_verification_report_yaml(path) == {"checked_on": "2026-11-20"}
    for text in ("notes: !!set {a, b}", "notes: .nan", "focus_areas: {1: {}}"):
        path.write_text(text)
        with pytest.raises(ValueError, match="JSON-compatible|mapping keys"):
            read_verification_report_yaml(path)


def test_unrelated_success_does_not_clear_failed_run(tmp_path):
    fixture, workspace, _, runtime, base_runtime, repo, digest, view = _setup_unbound_verifier(tmp_path)
    try:
        marker = tmp_path / "setup-complete"
        command = f"test -f {marker}"
        report_path = Path(workspace["verification_report_path"])
        report_path.write_text(_valid_report_yaml(candidate_digest=digest, command="git status --short"))
        for index, cmd in enumerate((command, "git status --short")):
            asyncio.run(runtime.execute_tool_async(new_tool_call(name="run_shell", args={"cmd": cmd}, call_id=str(index))))
        call = new_tool_call(name="op_bunshin_verification_pass", args={}, call_id="pass")
        rejected = swe_verification_tool_result(call, workspace, [])
        assert not rejected.ok and "unresolved failed verification check" in rejected.llm_text
        marker.touch()
        asyncio.run(runtime.execute_tool_async(new_tool_call(name="run_shell", args={"cmd": command}, call_id="retry")))
        report_path.write_text(_valid_report_yaml(candidate_digest=digest, command="never executed"))
        rejected = swe_verification_tool_result(call, workspace, [])
        assert not rejected.ok and "passing receipts" in rejected.llm_text

        report_path.write_text(_valid_report_yaml(candidate_digest=digest, command=command))
        assert swe_verification_tool_result(call, workspace, []).ok
        submission = json.loads((tmp_path / "stage/verification_submission.json").read_text())
        submission["tool_receipts"] = submission["tool_receipts"][:-1]
        errors = semantic_verification_submission_errors(
            submission, work_view=view, changed_paths=[], current_case_paths=["tests/router/verifier/test_router.py"],
            corpus_scope=view["verification_corpus"], scratch_only=False, workspace=workspace,
        )
        assert any("unresolved failed verification check" in error for error in errors)
    finally:
        runtime._execution.runtime.shutdown()
        base_runtime.shutdown()
        fixture.tearDown()


def test_role_working_state_and_stale_revision_error_surface_finding_revisions_without_draft_status(tmp_path):
    fixture, workspace, _pack, runtime, base_runtime, _repo, _digest, _view = _setup_unbound_verifier(tmp_path)
    try:
        update_checklist_tool_result(
            new_tool_call(
                name="op_bunshin_update_checklist",
                args={"plan": [{"step": "audit router", "status": "completed"}]},
                call_id="cl-1",
            ),
            workspace,
        )
        created = edit_finding_tool_result(
            new_tool_call(
                name="op_bunshin_update_finding",
                args={
                    "finding_id": "router_empty_key",
                    "expected_revision": 0,
                    "finding_kind": "module_defect",
                    "priority": "p1",
                    "summary": "Empty route key raises KeyError instead of ValueError.",
                },
                call_id="f-1",
            ),
            workspace,
        )
        assert created.ok, created.llm_text

        working_state = render_work_item_context(workspace)
        assert "router_empty_key (rev 1)" in working_state

        stale = edit_finding_tool_result(
            new_tool_call(
                name="op_bunshin_update_finding",
                args={
                    "finding_id": "router_empty_key",
                    "expected_revision": 99,
                    "finding_kind": "module_defect",
                    "priority": "p1",
                    "summary": "Updated summary.",
                },
                call_id="f-2",
            ),
            workspace,
        )
        assert not stale.ok
        assert "current revision is 1" in stale.llm_text
        assert "read_verification_draft_status" not in stale.llm_text
    finally:
        runtime._execution.runtime.shutdown()
        base_runtime.shutdown()
        fixture.tearDown()


def test_scratch_only_report_binds_in_review_scratch(tmp_path, monkeypatch):
    # macOS exposes its temporary directory through a symlinked ancestor.
    temp_alias = tmp_path / "temp-alias"
    temp_root = tmp_path / "temp-root"
    temp_root.mkdir()
    temp_alias.symlink_to(temp_root, target_is_directory=True)
    with monkeypatch.context() as patch:
        patch.setattr(tempfile, "tempdir", str(temp_alias))
        fixture, workspace, _pack, runtime, base_runtime, _repo, _digest, _view = _setup_unbound_verifier(
            tmp_path, scratch_only=True,
        )
    try:
        assert workspace["verification_report_relpath"] == "review_scratch/verification_report.yaml"
        assert Path(workspace["verification_report_path"]).is_file()
        from tests.core_factory import build_core
        from pal.execution import register_with_core

        report = workspace["verification_report_path"]
        assert report in render_bunshin_task_prompt(_pack)
        core = build_core()
        register_with_core(core.context)
        core.publish_module_capabilities("execution")
        files = BunshinScopedExecutionRuntime(core.context.execution_runtime, ["op_file_read", "op_file_write"], workspace)
        try:
            async def exercise_files():
                read = await files.execute_tool_async(
                    new_tool_call(name="read_file", args={"file_path": report}), turn_id="scratch-report",
                )
                assert read.ok, read.llm_text
                files.commit_tool_delivery(turn_id="scratch-report", context_delivery=read.context_delivery, result_id="read")

                content = _valid_report_yaml(candidate_digest=_digest)
                written = await files.execute_tool_async(
                    new_tool_call(name="write_file", args={"file_path": report, "content": content}), turn_id="scratch-report",
                )
                assert written.ok, written.llm_text
                assert Path(report).read_text() == content
                sibling = str(Path(report).with_name("other.yaml"))
                denied = await files.execute_tool_async(new_tool_call(name="write_file", args={"file_path": sibling, "content": content}))
                assert not denied.ok and not Path(sibling).exists()
            asyncio.run(exercise_files())
        finally:
            files._execution.runtime.shutdown()
            core.context.execution_runtime.shutdown()
    finally:
        runtime._execution.runtime.shutdown()
        base_runtime.shutdown()
        fixture.tearDown()


def test_terminal_workflow_event_attaches_verification_report_yaml(tmp_path):
    import hashlib
    from pal.bunshin.contracts import ActionEnvelope, AggregateType
    from pal.bunshin.delivery import DeliveryService
    from pal.bunshin.orchestration import BunshinOutboxProcessor
    from tests.test_bunshin_direct import direct_workflow, node_for
    from pal.bunshin.workspace_git import _git

    service, _, workflow_id = direct_workflow(tmp_path)
    node = node_for(service, workflow_id)
    repo = Path(node.payload["workspace_path"])
    manifest = service.artifacts.read_json(node.payload["architecture_manifest_ref"])
    snapshot = service.artifacts.read_json(manifest["workspace_snapshot_ref"])
    (repo / "app.py").write_text("print('verified')\n", encoding="utf-8")
    _git(repo, "add", "app.py")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@localhost", "commit", "-m", "Verified change")
    commit = _git(repo, "rev-parse", "HEAD").strip()

    report_bytes = _valid_report_yaml(module="direct", candidate_digest=commit).encode("utf-8")
    report_artifact_ref = service.artifacts.put_bytes(
        report_bytes,
        artifact_type="VerificationReportArtifact",
        media_type="text/yaml",
    )
    verification_ref = service.artifacts.put_json(
        {
            "status": "pass",
            "module_name": "direct",
            "verification_report_path": "tests/direct/verifier/verification_report.yaml",
            "verification_report_ref": report_artifact_ref.to_dict(),
            "verification_report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        },
        artifact_type="VerificationArtifact",
    )
    delivery_ref = DeliveryService(service.runtime_root, service.artifacts).publish(
        workflow_id=workflow_id,
        workflow_key="direct",
        task_title="Attach report",
        repository=repo,
        commit_sha=commit,
        source_snapshot=snapshot,
        verification_ref=verification_ref,
    )
    workflow = service.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    service.repository.transitions.dispatch(
        ActionEnvelope(
            action_type="MARK_COMPLETED",
            workflow_id=workflow_id,
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id=workflow_id,
            actor="test",
            expected_version=workflow.version,
            idempotency_key="completed-with-report",
            payload={"result_artifact_ref": delivery_ref.to_dict()},
        )
    )
    events = []
    BunshinOutboxProcessor(service, publish_workflow_event=events.append).publication.publish_terminal(workflow_id)
    assert len(events) == 1
    attachments = events[0]["attachments"]
    assert len(attachments) == 2
    assert attachments[0]["mime_type"] == "text/x-patch"
    assert attachments[1]["file_name"] == "direct_verification_report.yaml"
    assert attachments[1]["mime_type"] == "text/yaml"
    assert Path(attachments[1]["path"]).read_bytes() == report_bytes


def test_failed_lsp_is_explained_but_cannot_replace_strong_evidence():
    from pal.bunshin.verification_readiness import final_verification_errors, report_evidence_errors

    from pal.bunshin.verification_report import validate_verification_report

    command = {"kind": "command", "tool_name": "run_shell", "args": {"cmd": "python -m unittest"}, "ok": True}
    lsp = {"kind": "lsp", "tool_name": "op_lsp_diagnostics", "call_id": "diagnostics", "ok": False}
    report = {"verdict_summary": "Tests passed.", "focus_areas": {
        "regressions": {"status": "pass", "commands": ["python -m unittest"]},
        "lsp": {"status": "unknown", "notes": "The language server was unavailable."},
    }}
    workspace = {"verification_report_focus_areas": ["regressions", "lsp"]}
    assert not final_verification_errors([command, lsp], outcome="pass", changed=False)
    assert not report_evidence_errors(report, [command, lsp], workspace)
    assert not validate_verification_report(report, outcome="pass")
    report["focus_areas"]["lsp"]["notes"] = ""
    assert any("lsp.notes" in error for error in report_evidence_errors(report, [command, lsp], workspace))
    lsp["ok"] = True
    assert final_verification_errors([lsp], outcome="pass", changed=False)
    report["focus_areas"]["regressions"]["commands"] = ["diagnostics"]
    assert report_evidence_errors(report, [command, lsp], workspace)


def test_coder_finding_resolves_failed_shell_receipt_from_accepted_verification(tmp_path):
    from pal.bunshin.verification import repair_bill_semantic_view

    fixture, workspace, _, runtime, base_runtime, repo, digest, view = _setup_unbound_verifier(tmp_path)
    try:
        command = "echo 'wrong route' >&2; exit 7"
        result = asyncio.run(runtime.execute_tool_async(new_tool_call(
            name="run_shell", args={"cmd": command, "cwd": str(repo)}, call_id="router-probe",
        )))
        assert "Verification call_id: router-probe" in result.llm_text
        workspace["bunshin"]["swe_verification_tool_contract"] = {
            "repair_path_owners": {"router": [{"kind": "file", "path": "router.py"}]},
        }
        Path(workspace["verification_report_path"]).write_text(_valid_report_yaml(candidate_digest=digest))
        fields = {
            "finding_id": "wrong-route", "expected_revision": 0, "finding_kind": "module_defect",
            "summary": "The router selects the wrong destination.", "priority": "p1",
            "locations": [{"scope": "workspace", "file": "router.py", "line": 1}],
            "evidence_call_id": "router-probe",
        }
        finding = asyncio.run(runtime.execute_tool_async(new_tool_call(name="update_finding", args=fields)))
        assert finding.ok, finding.llm_text
        submit = new_tool_call(name="op_bunshin_verification_request_module_repair", args={})
        result = swe_verification_tool_result(submit, workspace, [])
        assert result.ok, result.llm_text
        submission = json.loads((tmp_path / "stage/verification_submission.json").read_text())
        receipts = submission["tool_receipts"]
        store = fixture.store
        receipt_set = store.put_json({"receipts": receipts}, artifact_type="VerificationToolReceiptSetArtifact")
        packet = store.put_json({
            "artifact_kind": "semantic_repair_packet", "module_name": "router",
            "findings": submission["findings"], "tool_receipts_ref": receipt_set.to_dict(),
        }, artifact_type="RepairPacketArtifact")
        item, = repair_bill_semantic_view(store, packet, module_name="router")["findings"]
        assert item["evidence_call_id"] == "router-probe"
        execution = item["execution"]
        assert execution["args"] == {"cmd": command, "cwd": str(repo)}
        assert execution["returncode"] == 7 and execution["ok"] is False
        assert execution["stderr"] == "wrong route\n"

        def validate():
            return semantic_verification_submission_errors(
                submission, work_view=view, changed_paths=[], current_case_paths=["tests/router/verifier/test_router.py"],
                corpus_scope=view["verification_corpus"], scratch_only=False, workspace=workspace,
            )

        assert not validate()
        receipts.append(dict(receipts[0]))
        assert any("must identify one execution" in msg for msg in validate())
        receipts.pop()
        receipts[0]["verification_binding"]["candidate_digest"] = "different-candidate"
        assert any("different Candidate" in msg for msg in validate())
        receipts[0]["verification_binding"]["candidate_digest"] = digest
        submission["findings"][0]["evidence_call_id"] = "unrelated"
        assert any("unrelated" in msg for msg in validate())
        del submission["findings"][0]["evidence_call_id"]
        assert not validate()
    finally:
        runtime._execution.runtime.shutdown()
        base_runtime.shutdown()
        fixture.tearDown()


@pytest.mark.parametrize("scope", ["module", "sink"])
def test_verdict_accepts_one_fresh_complete_suite_without_report_or_checklist(tmp_path, scope):
    from pal.bunshin.repair_precheck import suite_command

    fixture, workspace, _, runtime, base, repo, _, view = _setup_unbound_verifier(
        tmp_path, verdict_scope=scope, with_test_file=False)
    try:
        corpus = view["verification_corpus"]
        runner = f"{corpus['path']}/run-tests.sh"
        script = repo / runner
        # Exercise a real consumer via stdin and stdout, including an empty input.
        (repo / "src/router.py").write_text("import sys\nprint(sys.stdin.read().strip().upper())\n")
        subprocess.run(["git", "-C", str(repo), "commit", "-qam", "add CLI"], check=True)
        workspace["verification_candidate_digest"] = subprocess.check_output(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
        script.write_text('set -eu\ntest "$(printf hello | python src/router.py)" = HELLO\n'
                          'test "$(python src/router.py </dev/null)" = ""\n')
        command = suite_command(corpus, [runner])

        def run(command, call_id):
            return asyncio.run(runtime.execute_tool_async(new_tool_call(
                name="run_shell", args={"cwd": str(repo), "cmd": command}, call_id=call_id)))

        def submit(**args):
            return asyncio.run(runtime.execute_tool_async(new_tool_call(name="submit_verification_pass", args=args)))

        assert run("git status --short", "status").ok
        assert "complete verifier suite" in submit().llm_text
        assert run(command, "first").ok
        script.write_text(script.read_text() + "# added a case\n")
        assert "complete verifier suite" in submit(system_test_calls=["first"]).llm_text
        assert run(command, "suite").ok
        if scope == "sink":
            assert "system_test_calls" in submit().llm_text
            for invalid in ["first", "unknown"]:
                assert not submit(system_test_calls=[invalid]).ok
        passed = submit(**({"system_test_calls": ["suite"]} if scope == "sink" else {}))
        assert passed.ok, passed.llm_text
        sub = json.loads((tmp_path / "stage/verification_submission.json").read_text())
        assert sub["work_items"] == []
        assert "verification_report" not in sub
        assert not (script.parent / VERIFICATION_REPORT_FILENAME).exists()

        def manager_errors(submission):
            return semantic_verification_submission_errors(
                submission, work_view=view, changed_paths=[runner], current_case_paths=[runner],
                corpus_scope=corpus, scratch_only=False, workspace=workspace)
        assert not manager_errors(sub)
        if scope == "sink":
            assert manager_errors({**sub, "system_test_calls": []})
            assert manager_errors({**sub, "system_test_calls": ["first"]})
        script.write_text("exit 1\n")
        assert manager_errors(sub)
    finally:
        runtime._execution.runtime.shutdown()
        base.shutdown()
        fixture.tearDown()


def test_verdict_returns_static_counterexamples_together_without_a_green_suite(tmp_path):
    fixture, workspace, _, runtime, base, repo, _, view = _setup_unbound_verifier(
        tmp_path, verdict_scope="module", with_test_file=False)
    try:
        workspace["bunshin"]["swe_verification_tool_contract"] = {
            "repair_path_owners": {"router": [{"kind": "file", "path": "src/router.py"}]}}
        for name, example in [("blank", "Empty input returns an empty route instead of raising."),
                              ("case", "Input ABC returns ABC instead of abc.")]:
            result = asyncio.run(runtime.execute_tool_async(new_tool_call(name="update_finding", args={
                "finding_id": name, "expected_revision": 0, "finding_kind": "module_defect",
                "summary": example, "priority": "p1",
                "locations": [{"scope": "workspace", "file": "src/router.py", "line": 1}],
            })))
            assert result.ok, result.llm_text
        assert "case (rev 1)" in render_work_item_context(workspace)
        result = swe_verification_tool_result(new_tool_call(name="op_bunshin_verification_request_module_repair"),
                                              workspace, [])
        assert result.ok, result.llm_text
        sub = json.loads((tmp_path / "stage/verification_submission.json").read_text())
        assert {f["finding_id"] for f in sub["findings"]} == {"blank", "case"}
        assert not semantic_verification_submission_errors(
            sub, work_view=view, changed_paths=[], current_case_paths=[], corpus_scope=view["verification_corpus"],
            scratch_only=False, workspace=workspace)
    finally:
        runtime._execution.runtime.shutdown()
        base.shutdown()
        fixture.tearDown()


def test_verdict_distinguishes_consumer_inputs_and_explains_weak_lsp():
    from pal.bunshin.verification_readiness import verdict_pass_errors

    corpus = {"kind": "directory", "path": "cases"}
    workspace = {"repair_suite_required": True, "verification_verdict_scope": "sink", "repo_path": "/repo"}
    receipts = [
        {"kind": "command", "tool_name": "op_exec_shell", "args": {"cmd": cmd, "stdin": stdin}, "ok": True, "call_id": key}
        for key, cmd, stdin in [("suite", "sh cases/run-tests.sh", None), ("empty", "cli", ""), ("value", "cli", "1")]
    ]
    receipts.append({"kind": "lsp", "tool_name": "op_lsp_diagnostics", "ok": False,
                     "args": {"file_path": "module.py"}, "call_id": "lsp"})

    def errors(note):
        return verdict_pass_errors(workspace, receipts, scope=corpus, case_paths=["cases/run-tests.sh"],
                                   system_test_calls=["empty", "value"], note=note)

    assert any("LSP" in error for error in errors(""))
    assert not errors("Language server could not resolve the dependency. CLI scenarios passed.")
    receipts.append({**receipts[0], "ok": False, "call_id": "suite-again"})
    assert any("complete verifier suite" in error for error in errors("Language server unavailable."))


def test_verdict_ignores_a_legacy_report_left_in_scratch():
    from pal.bunshin.verification_readiness import verdict_pass_errors

    workspace = {"verification_verdict_scope": "module", "verification_scratch_only": True}
    receipt = {"kind": "command", "tool_name": "op_exec_shell", "args": {"cmd": "check"}, "ok": True}
    def verify(paths):
        return verdict_pass_errors(workspace, [receipt], scope={}, case_paths=paths, system_test_calls=[], note="")

    assert verify(["review_scratch/verification_report.yaml"])
    assert not verify(["review_scratch/cases/check.sh", "review_scratch/verification_report.yaml"])

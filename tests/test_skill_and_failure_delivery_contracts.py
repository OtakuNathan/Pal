"""Verify model-visible skill data, failure causes and post-commit effects."""
import errno
import json
import os
import stat

import pytest

from pal.bunshin.ipc import BunshinManagerRpcError
from pal.bunshin.workflow_capabilities import BunshinPublicProvider
from pal.core import PalCore
from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
from pal.execution import register_with_core as register_execution
from pal.foundation import PalV2Database
from pal.skill.capabilities import register_with_core as register_skill
from pal.skill.contracts import SkillDescriptor
from pal.skill.models import SkillModel
from pal.skill.service import SkillService
from tests.test_tool_failure_affordances import invoke, metadata, read


@pytest.fixture
def core(tmp_path):
    core = PalCore()
    register_execution(core.context)
    core.publish_module_capabilities("execution")
    execution = core.context.execution_runtime
    execution.configure_runtime_root(tmp_path / "runtime")
    execution.begin_tool_result_turn(turn_id="review", scope_key="failure-review")
    yield core
    execution.shutdown()


@pytest.fixture
def runtime(core):
    return core.context.execution_runtime


@pytest.fixture
def skill_provider(core, runtime, tmp_path):
    db = PalV2Database(tmp_path / "skills.sqlite3")
    db.initialize([SkillModel])
    service = SkillService(runtime_root=tmp_path, execution_runtime=runtime)
    handle = register_skill(core.context, service)
    core.publish_module_capabilities("skill")
    provider = handle.introspection_provider
    yield provider
    db.close()


@pytest.mark.parametrize("operation", ["read_skill", "inject_skill"])
def test_skill_manual_and_metadata_preserve_literal_values(runtime, skill_provider, operation):
    manual = 'Send external mode="op_tool_read" exactly. Open /tmp/op_tool_read.json and /tmp/op_external_payload.json.'
    skill = SkillDescriptor(skill_id="literal.manual", module_id="audit", title="Literal contract",
        summary="Keep external protocol values unchanged.", manual_text=manual,
        metadata={"external_schema": {"const": "op_tool_read", "default": "op_external_payload"}},
        source_refs=("/tmp/op_external_payload.json",), capability_refs=("op_tool_read",))
    skill_provider.service.repository.upsert_skill(skill)
    args = {"name": skill.skill_id, **({"include_manual": True} if operation == "read_skill" else {})}
    result = invoke(runtime, operation, args)
    assert result.ok, result.llm_text
    delivered = result.llm_text if operation == "read_skill" else result.context_messages[0].content
    if operation == "read_skill":
        visible = json.loads(delivered.split("\n", 1)[1].rsplit("\n\nTool result metadata:", 1)[0])["skill"]
        assert visible["manual_text"] == manual
        assert visible["metadata"] == skill.metadata
        assert visible["source_refs"] == list(skill.source_refs)
        assert visible["capability_refs"] == ["read_tool"]
    else:
        assert manual in delivered
        assert "Capability refs: read_tool" in delivered
    stored = skill_provider.service.repository.get_skill(skill.skill_id)
    assert stored.manual_text == manual


def test_skill_name_remains_usable(runtime, skill_provider):
    skill_provider.service.repository.upsert_skill(SkillDescriptor(skill_id="op_tool_read", module_id="audit",
        title="Literal skill identity", summary="A valid stored skill.", manual_text="Keep this identity."))
    result = invoke(runtime, "read_skill", {"name": "op_tool_read"})
    assert result.ok
    visible = json.loads(result.llm_text.split("\n", 1)[1].rsplit("\n\nTool result metadata:", 1)[0])
    shown_name = visible["skill"]["skill_id"]
    injected = invoke(runtime, "inject_skill", {"name": shown_name})
    assert shown_name == "op_tool_read"
    assert injected.ok


@pytest.mark.parametrize("operation,state", [("read_skill", "missing"), ("inject_skill", "missing"),
                                             ("inject_skill", "disabled")])
def test_skill_precondition_failure_reports_no_effect(runtime, skill_provider, operation, state):
    if state == "disabled":
        skill_provider.service.repository.upsert_skill(SkillDescriptor(skill_id="blocked.skill", module_id="audit",
            title="Disabled skill", summary="Unavailable for injection.", manual_text="Do not inject.",
            status="disabled", enabled=False))
    before = tuple(skill.to_dict() for skill in skill_provider.service.repository.list_skills())
    result = invoke(runtime, operation, {"name": "blocked.skill"})
    after = tuple(skill.to_dict() for skill in skill_provider.service.repository.list_skills())
    assert not result.ok and not result.context_messages
    assert metadata(result)["effect"] == "none"
    assert metadata(result)["retry"] == "safe"
    assert before == after


@pytest.mark.parametrize("operation", ["catalog", "archive", "archive_error"])
def test_bunshin_handler_preserves_original_exception_chain(core, runtime, tmp_path, monkeypatch, operation):
    def fail(*args, **kwargs):
        try:
            raise PermissionError("root cause: cannot read pinned family file")
        except PermissionError as exc:
            if operation == "catalog":
                raise BunshinManagerRpcError("manager request failed", kind="transport", payload={"failed_stage": "catalog decode", "token": "private-audit-token"}) from exc
            if operation == "archive_error":
                raise RuntimeError("archive backend wrapper") from exc
            raise ValueError("archive backend wrapper") from exc
    provider = BunshinPublicProvider(tmp_path, manager_request=fail)
    if operation != "catalog":
        monkeypatch.setattr(provider.service, "resolve_task_workflow_selector", lambda **kwargs: ("task", "workflow"))
        monkeypatch.setattr(provider.service, "archive_workflow", fail)
    core.context.register_module(ModuleHandle(module_id="bunshin", tier=MODULE_TIER_DETACHABLE,
        detachable=True, introspection_provider=provider))
    core.publish_module_capabilities("bunshin")
    alias = "read_bunshin_catalog" if operation == "catalog" else "archive_bunshin_workflow"
    result = invoke(runtime, alias, {} if operation == "catalog" else {"task": "audit"})
    assert not result.ok
    assert "root cause: cannot read pinned family file" in result.llm_text
    assert "PermissionError" in result.llm_text
    if operation == "catalog":
        assert "catalog decode" in result.llm_text
        assert "manager request failed" in result.llm_text
        assert "private-audit-token" not in result.llm_text
        assert "[redacted]" in result.llm_text


@pytest.mark.parametrize("stage", ["open", "fsync", "close"])
@pytest.mark.parametrize("operation", ["write_file", "overwrite_file", "edit_file"])
def test_directory_sync_failure_reports_applied_write(runtime, tmp_path, monkeypatch, operation, stage):
    original_open = os.open
    failures = []
    def fail_directory_open(path, flags, *args, **kwargs):
        if flags & os.O_DIRECTORY:
            failures.append(str(path))
            if stage == "open":
                raise OSError(errno.EIO, "injected directory sync failure", str(path))
        return original_open(path, flags, *args, **kwargs)
    path = tmp_path / "written.txt"
    if operation != "write_file":
        path.write_text("original")
        read(runtime, path)
    monkeypatch.setattr("pal.execution.file_state.os.open", fail_directory_open)
    original_fsync, original_close = os.fsync, os.close
    def fail_directory_fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) and stage == "fsync":
            raise OSError(errno.EIO, "injected directory sync failure")
        return original_fsync(fd)
    def fail_directory_close(fd):
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        original_close(fd)
        if is_directory and stage == "close":
            raise OSError(errno.EIO, "injected directory sync failure")
    monkeypatch.setattr("pal.execution.file_state.os.fsync", fail_directory_fsync)
    monkeypatch.setattr("pal.execution.file_state.os.close", fail_directory_close)
    args = {"file_path": str(path), "content": "written"} if operation != "edit_file" else {
        "file_path": str(path), "edits": [{"old_string": "original", "new_string": "written"},
                                              {"old_string": "absent", "new_string": "unused"}]}
    result = invoke(runtime, "write_file" if operation == "overwrite_file" else operation, args)
    assert failures == [str(tmp_path)]
    assert not result.ok
    assert metadata(result)["effect"] == "applied"
    assert metadata(result)["error_code"] == ("DIRECTORY_CLOSE_FAILED" if stage == "close" else "DURABILITY_FAILED")
    assert result.invocation_result.details["directory_synced"] == (stage == "close")
    if stage == "close":
        assert "crash durability is unconfirmed" not in result.llm_text
    assert metadata(result)["retry"] == "do_not_retry"
    assert path.read_text() == "written"
    assert "injected directory sync failure" in result.llm_text
    assert "Do not repeat the applied write" in result.llm_text
    if operation == "edit_file":
        assert result.invocation_result.details["applied_edit_indices"] == [0]
        assert result.invocation_result.details["failed_edits"][0]["edit_index"] == 1
        assert "NOT_FOUND_MATCH" in result.llm_text
        assert '"applied_edit_indices":[0]' in result.llm_text

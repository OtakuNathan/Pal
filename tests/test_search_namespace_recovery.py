"""Search recovery keeps strict hits separate from alternate namespaces."""

import pytest

from pal.checklist.capabilities import register_with_core as register_checklist
from pal.checklist.service import ChecklistService
from pal.execution import register_with_core as register_execution
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, ToolGuidance
from pal.proactive import ProactiveManager, register_with_core as register_proactive
from tests.capability_fixture import mount_test_capability
from tests.core_factory import build_core
from tests.test_tool_search_rules import search


@pytest.fixture
def runtime():
    core = build_core()
    try:
        register_execution(core.context)
        register_checklist(core.context, ChecklistService())
        register_proactive(core.context, ProactiveManager())
        for owner in ("execution", "checklist", "proactive"):
            core.publish_module_capabilities(owner)
        runtime = core.context.execution_runtime
        mount_test_capability(
            runtime, alias="read_shell_session", canonical_path="op_test_read_shell_session",
            module_id="execution", metadata={"namespace": "operation"},
            InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: {},
            guidance=ToolGuidance(purpose="Read retained session output.", use_when="A session ID is known.",
                                  do_not_use_when="Starting a new command.", search_domain="shell"),
        )
        yield runtime
    finally:
        core.close()


@pytest.mark.parametrize(("domain", "query", "alias"), [
    ("checklist", "read active checklist progress", "read_checklist"),
    ("shell", "read retained shell session output by session id", "read_shell_session"),
    ("", "read_checklist", "read_checklist"),
])
def test_namespace_miss_returns_an_actionable_contract_not_a_hit(runtime, domain, query, alias):
    result = search(runtime, query, domain=domain, namespace="inspect", top_k=1)
    assert result["hits"] == []
    assert result["total_count"] == result["returned_count"] == 0
    assert result["applied_filters"]["namespace"] == "introspection"
    [suggestion] = result["suggestions"]
    assert suggestion["alias"] == suggestion["contract"]["alias"] == alias
    assert suggestion["contract"]["namespace"] == "operation"
    assert suggestion["contract"]["invocation_mode"] == "indirect"
    assert "input_contract" in suggestion["contract"]
    assert "namespace" in result["usage_hint"]
    assert "directory" not in result


def test_namespace_recovery_does_not_relax_other_filters_or_browse(runtime):
    for filters in ({"domain": "proactive"}, {"family": "missing"},
                    {"module_name": "missing"}, {"tags": ["missing"]}):
        result = search(runtime, "read_checklist", namespace="inspect",
                        **{"domain": "checklist", **filters})
        assert not result["hits"] and not result.get("suggestions")
    assert not search(runtime, "", domain="checklist", namespace="inspect").get("suggestions")


def test_scheduler_health_ranks_module_health_ahead_of_single_task(runtime):
    for namespace in ("", "inspect"):
        result = search(runtime, "inspect scheduler module health", domain="proactive",
                        **({"namespace": namespace} if namespace else {}), top_k=1)
        assert result["hits"][0]["alias"] == "inspect_proactive_status"
        assert not result.get("suggestions")

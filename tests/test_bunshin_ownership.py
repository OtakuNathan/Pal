"""Ownership boundaries and size budgets for Bunshin."""
from __future__ import annotations

import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'src/pal/bunshin'
BASELINE = Path(__file__).with_name('fixtures') / 'bunshin_ownership.json'


def measurements(path: Path) -> dict[str, int]:
    source = path.read_text()
    tree = ast.parse(source)
    sizes = {'file': len(source.splitlines())}
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = 'class' if isinstance(node, ast.ClassDef) else 'function'
            sizes[f'{kind}:{node.name}:{node.lineno}'] = node.end_lineno - node.lineno + 1
    return sizes


def owned_modules() -> set[Path]:
    declared = {PACKAGE / name for name in json.loads(BASELINE.read_text())}
    for directory in ("runner_components", "storage", "semantic_orchestration"):
        declared.update((PACKAGE / directory).rglob("*.py"))
    return declared


def test_bunshin_ownership_budgets():
    limits = {"file": 800, "class": 400, "function": 150}
    entries = {"BunshinRunner", "BunshinV2Repository", "SemanticOrchestrator"}
    for path in sorted(owned_modules()):
        for key, size in measurements(path).items():
            kind, _, symbol = key.partition(":")
            name = symbol.rsplit(":", 1)[0]
            maximum = 300 if kind == "class" and name in entries else limits[kind]
            assert size <= maximum, f"{path.relative_to(PACKAGE)}:{key}: {size} > {maximum}"


def test_components_never_import_entrypoints():
    entries = {
        "pal.bunshin.runner",
        "pal.bunshin.execution",
        "pal.bunshin.semantic_orchestration.orchestrator",
    }
    for path in owned_modules():
        if path.name in {"__init__.py", "runner.py", "execution.py", "orchestrator.py"}:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in entries, (path, node.module)


def test_storage_cannot_import_application_components():
    forbidden = ("pal.bunshin.runner", "pal.bunshin.semantic_orchestration", "pal.bunshin.service")
    for path in (PACKAGE / "storage").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(forbidden), (path, node.module)


def test_workspace_resources_do_not_depend_on_execution():
    tree = ast.parse((PACKAGE / 'workspace_resources.py').read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module not in {
                'pal.bunshin.execution',
                'pal.bunshin.semantic_orchestration.orchestrator',
            }


def test_unit_of_work_publishes_or_rolls_back_all_stores(tmp_path):
    import pytest
    from pal.bunshin.contracts import ActionEnvelope, AggregateType
    from pal.bunshin.cycle_protocol import PlanCycle
    from pal.bunshin.repository import BunshinV2Repository

    repository = BunshinV2Repository(tmp_path)
    action = ActionEnvelope(
        action_type="CREATE_WORKFLOW", workflow_id="wf-atomic",
        aggregate_type=AggregateType.WORKFLOW, aggregate_id="wf-atomic",
        actor="test", payload={}, idempotency_key="create",
    )
    with pytest.raises(RuntimeError, match="abort transition"):
        with repository.transaction() as work:
            work.transitions.dispatch(action)
            work.cycles.store_plan_cycle(workflow_id="wf-atomic", cycle=PlanCycle(cycle_id="plan"))
            assert work.snapshots.read_snapshot(AggregateType.WORKFLOW, "wf-atomic") is not None
            assert repository.snapshots.read_snapshot(AggregateType.WORKFLOW, "wf-atomic") is None
            raise RuntimeError("abort transition")
    assert repository.snapshots.read_snapshot(AggregateType.WORKFLOW, "wf-atomic") is None
    assert repository.cycles.read_plan_cycle(workflow_id="wf-atomic") is None
    with repository.transaction() as work:
        result = work.transitions.dispatch(action)
        assert not result.duplicate  # Rollback includes idempotency and outbox rows.
        work.cycles.store_plan_cycle(workflow_id="wf-atomic", cycle=PlanCycle(cycle_id="plan"))
    assert repository.snapshots.read_snapshot(AggregateType.WORKFLOW, "wf-atomic") is not None
    assert repository.cycles.read_plan_cycle(workflow_id="wf-atomic") is not None
    with pytest.raises(RuntimeError, match="already closed"):
        work.snapshots.read_snapshot(AggregateType.WORKFLOW, "wf-atomic")


def test_runner_state_is_isolated_between_invocations(tmp_path):
    from pal.bunshin.runner import BunshinRunner
    from pal.shared import BunshinInvocationPack

    async def sink(*_args):
        return None

    first = BunshinRunner(tmp_path, BunshinInvocationPack(invocation_id="one"), "one", "one", sink, sink)
    second = BunshinRunner(tmp_path, BunshinInvocationPack(invocation_id="two"), "two", "two", sink, sink)
    first.components.status.block("waiting for approval")
    first.components.research_budget.restore({"total": 3})
    first.components.artifacts.restore([{"path": "first.json"}])
    first.components.tool_session.observe_count(4)
    assert second.components.status.blocked_summary == ""
    assert second.components.research_budget.web_research_usage == {}
    assert second.components.artifacts.produced_artifacts == []
    assert second.components.tool_session.observed_tool_call_count == 0


def test_semantic_components_do_not_import_workflow_facade():
    for path in (PACKAGE / "semantic_orchestration").glob("*.py"):
        if path.name in {"composition.py", "orchestrator.py"}:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "pal.bunshin.service", path

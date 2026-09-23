from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pal.control.contracts import ControlRoute
from pal.control.presentation import interaction_projection
from pal.memory.contracts import L3BatchCommitRequest, L3CommitRequest, L3DeleteRequest
from pal.memory.embedding import HashingEmbedder
from pal.memory.proposals import MemoryProposalBatch, normalize_memory_candidates
from pal.memory.repository import L3ProviderSelector
from pal.memory.review import MemoryReviewService
from pal.memory.service import MemoryService
from pal.memory.storage import MemoryStorage
from pal.plugins.l3.sqlite_vec import SQLiteVecL3Plugin

ROUTE = ControlRoute("tty", "socket", {"session_id": "first"})
TEXT = "def f():\n    return 'a,}'\n"
FACT = {"kind": "fact", "title": "API preference", "summary": TEXT,
        "source_excerpt": "Keep explicit public APIs.", "topics": ["API"]}


@pytest.fixture
def setup_review(tmp_path):
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    repo = storage.open()
    memory = MemoryService(review_storage=storage)
    provider = SQLiteVecL3Plugin(service=memory, repository=repo, embedder=HashingEmbedder())
    memory.l3_selector = L3ProviderSelector(resolver=lambda _: provider, active_provider_id=provider.provider_id)
    yield memory.reviews, provider, storage
    repo.close()


def stage(reviews, *, candidates=None, source=None, identity="batch", route=ROUTE):
    return reviews.stage(MemoryProposalBatch(identity, tuple(candidates or [FACT]), source or {}), route)


def decision(reviews, state, operation, **args):
    return reviews.apply(state["batch_id"], {"revision": state["revision"], "decision": operation, **args}, ROUTE)


def test_replayed_proposal_checks_owner_before_returning_draft(setup_review):
    reviews, _, _ = setup_review
    state = stage(reviews)
    with pytest.raises(ValueError, match="endpoint"):
        stage(reviews, route=replace(ROUTE, endpoint_id="another"))
    restored = stage(reviews, route=replace(ROUTE, reply_target={"session_id": "reconnected"}))
    assert restored["batch_id"] == state["batch_id"]
    assert restored["drafts"] == state["drafts"]


def test_review_entry_opens_preview_and_advances_to_next_pending_candidate(setup_review):
    from pal.control import ControlPlane
    reviews, _, _ = setup_review
    state = stage(reviews, candidates=[FACT, {**FACT, "title": "Second"}])
    spec = reviews.delivery(state, ROUTE, opening=True).interaction
    assert f"/memory_review {state['batch_id']}" in spec.text
    preview = spec
    assert len(preview.items) == 1
    assert preview.items[0].item_id == "c1"
    assert [button.label for button in preview.items[0].buttons[0]] == ["Accept", "Reject", "Edit"]
    assert "Candidate 1 of 2" in spec.text
    assert FACT["source_excerpt"] in preview.items[0].text
    assert FACT["summary"] in preview.items[0].text
    state = decision(reviews, state, "accept", candidate_id="c1")
    next_spec = reviews.delivery(state, ROUTE).interaction
    assert len(next_spec.items) == 1
    next_button = next_spec.items[0].buttons[0][0]
    assert next_button.action_args["args"]["candidate_id"] == "c2"
    resumed = reviews.get(state["batch_id"], ROUTE, resume=True)
    assert reviews.delivery(resumed, ROUTE).interaction.items[0].item_id == "c2"
    panel = ControlPlane().list_panel_commands()
    assert any(item.name == "memory_review" and item.panel_button and item.panel_label == "Memory proposals" for item in panel)


def test_completed_review_open_reports_result_without_requiring_existing_card(setup_review):
    reviews, _, _ = setup_review
    empty = stage(reviews, candidates=[{"kind": "fact"}], identity="invalid")
    delivery = reviews.delivery(empty, ROUTE, opening=True)
    assert delivery.delivery_kind == "reply"
    assert "format issues" in delivery.text
    state = decision(reviews, stage(reviews), "skip", candidate_id="c1")
    state = decision(reviews, state, "submit")
    assert reviews.delivery(state, ROUTE).delivery_kind == "interactive_resolve"
    reopened = reviews.delivery(reviews.get(state["batch_id"], ROUTE), ROUTE, opening=True)
    assert reopened.delivery_kind == "reply"
    assert "1 skipped" in reopened.text


def test_mark_edit_then_submit_is_the_only_write_authority(setup_review):
    reviews, provider, _ = setup_review
    state = stage(reviews)
    with pytest.raises(ValueError, match="authorization"):
        reviews.commit(state["batch_id"])
    state = decision(reviews, state, "accept", candidate_id="c1")
    assert provider.repository.list_projection_rows() == []
    with pytest.raises(ValueError):
        reviews.commit(state["batch_id"])
    state = decision(reviews, state, "save", candidate_id="c1", field="summary", input_values={"value": TEXT + "\n"})
    assert state["drafts"][0]["decision"] == "pending"
    with pytest.raises(ValueError):
        decision(reviews, state, "submit")
    state = decision(reviews, state, "accept", candidate_id="c1")
    state = decision(reviews, state, "submit")
    result = reviews.commit(state["batch_id"])
    assert result["status"] == "ok"
    assert provider.repository.get_document(result["references"][0])["summary"] == TEXT + "\n"
    assert reviews.get(state["batch_id"], ROUTE)["drafts"] == []
    assert reviews.commit(state["batch_id"])["references"] == result["references"]


def test_batch_conflict_rolls_back_body_fts_pending_and_receipts(setup_review):
    _, provider, _ = setup_review
    existing = L3CommitRequest(kind="fact", title="existing", summary="before", search_text="before", canonical_key="key")
    provider.commit(existing)
    batch = L3BatchCommitRequest("atomic", (
        replace(existing, canonical_key=None, title="new", summary="new", search_text="new", mutation_id="a"),
        replace(existing, summary="after", mutation_id="b")))
    before = list(provider.service.l2_store.items)
    result = provider.commit_batch(batch)
    assert result.status == "conflict"
    assert len(provider.repository.list_projection_rows()) == 1
    assert provider.repository.database.execute_sql("SELECT count(*) FROM memory_batch_receipts").fetchone()[0] == 0
    assert provider.repository.database.execute_sql("SELECT count(*) FROM memory_mutations WHERE mutation_id IN ('a','b')").fetchone()[0] == 0
    assert list(provider.service.l2_store.items) == before


def test_receipt_reconciles_crash_after_commit_and_never_resurrects_deleted(setup_review):
    reviews, provider, storage = setup_review
    state = decision(reviews, stage(reviews), "accept", candidate_id="c1")
    state = decision(reviews, state, "submit")
    original_save = reviews._save
    def crash(db, value):
        if value["status"] == "completed":
            raise RuntimeError("crash after L3 commit")
        original_save(db, value)
    with patch.object(reviews, "_save", side_effect=crash):
        with pytest.raises(RuntimeError):
            reviews.commit(state["batch_id"])
    restored = MemoryReviewService(reviews.memory, storage)
    recovered = restored.get(state["batch_id"], ROUTE)
    assert recovered["status"] == "completed"
    assert len(provider.repository.list_projection_rows()) == 1
    ref = recovered["result"]["references"][0]
    provider.delete(L3DeleteRequest(ref))
    restored.commit(state["batch_id"])
    assert provider.repository.list_projection_rows() == []


def test_skip_all_stale_buttons_and_reconnect_owner(setup_review):
    reviews, provider, _ = setup_review
    initial = stage(reviews)
    state = decision(reviews, initial, "skip", candidate_id="c1")
    with pytest.raises(ValueError, match="changed"):
        decision(reviews, initial, "accept", candidate_id="c1")
    wrong = replace(ROUTE, endpoint_id="other")
    with pytest.raises(ValueError):
        reviews.get(state["batch_id"], wrong)
    resumed = reviews.get(state["batch_id"], replace(ROUTE, reply_target={"session_id": "new"}), resume=True)
    assert resumed["revision"] > state["revision"]
    state = decision(reviews, resumed, "submit")
    assert state["status"] == "completed"
    assert provider.repository.list_projection_rows() == []


def test_no_final_button_before_all_decisions_and_only_refs_in_wire(setup_review):
    reviews, _, _ = setup_review
    state = stage(reviews, candidates=[FACT, {**FACT, "title": "second"}])
    spec = reviews.delivery(state, ROUTE).interaction
    wire, actions = interaction_projection(spec)
    assert wire["buttons"] == []
    assert len(wire["items"]) == 1
    assert all("action_args" not in item for row in wire["items"][0]["buttons"] for item in row)
    assert all("memory_candidates" not in action["action_args"] for action in actions.values())
    state = decision(reviews, state, "accept", candidate_id="c1")
    state = decision(reviews, state, "skip", candidate_id="c2")
    assert reviews.delivery(state, ROUTE).interaction.buttons[0][0].label == "Submit accepted items"
    assert reviews.delivery(state, ROUTE).interaction.items == ()


def test_legacy_candidates_retained_but_require_new_decisions(setup_review):
    reviews, _, _ = setup_review
    state = reviews.stage(MemoryProposalBatch("legacy", tuple({**FACT, "title": str(i)} for i in range(8))), ROUTE, legacy=True)
    assert len(state["drafts"]) == 8
    assert {draft["decision"] for draft in state["drafts"]} == {"pending"}


def test_bunshin_task_binding_and_forgetting_pending_source(setup_review):
    reviews, provider, _ = setup_review
    ref = provider.commit(L3CommitRequest(kind="fact", title="source", summary="source", search_text="source")).document_id
    state = stage(reviews, source={"source_kind": "bunshin", "task_id": "real-task", "memory_refs": [ref]},
                  candidates=[{**FACT, "task_id": "invented-task"}])
    state = decision(reviews, state, "accept", candidate_id="c1")
    assert reviews._requests(state)[0].task_id == "real-task"
    provider.delete(L3DeleteRequest(ref))
    expired = reviews.get(state["batch_id"], ROUTE)
    assert expired["result"]["invalidated"]
    assert not expired["drafts"]
    assert provider.repository.list_projection_rows() == []


def test_candidate_normalization_never_invents_case_or_changes_strings():
    items, diagnostics = normalize_memory_candidates([
        {**FACT, "unknown": "private"}, {**FACT, "kind": "case"}, None,
        *[{**FACT, "title": str(i)} for i in range(8)]])
    assert len(items) == 5
    assert items[0]["summary"] == TEXT
    assert all(item["kind"] == "fact" for item in items)
    assert "private" not in str(diagnostics)
    assert any("star:required_fields" in item for item in diagnostics)


def _left_snapshot(service, metadata=None):
    """v3 capture: a real begin_left_compaction run over the current left."""
    from pal.core.compaction import CompactionClockKind, CompactionSnapshot
    root = service.history_root
    if not root.left_messages():
        root.promote()
    run = service.begin_left_compaction("review-run", reason="review")
    return CompactionSnapshot.capture_left(
        service, run,
        target_input_budget=8_192, reserved_output_tokens=2_048,
        clock_kind=CompactionClockKind.USER_TURN, clock_value=7,
        metadata=metadata,
    )


def test_compact_tolerates_wrappers_and_bad_optional_candidates():
    import asyncio
    import json
    from pal.core.compaction import CompactionEngine, extract_json_object
    from pal.core.pal_compaction import PalCompactionPolicy
    from pal.llm import generation_result_from_values
    from tests.test_runtime_compaction import _ScriptedLLM, _memory_with_turns, _valid_pal_payload
    payload = json.loads(_valid_pal_payload())
    payload["memory_candidates"] = [FACT, {**FACT, "kind": "case"}]
    raw = "Here is the JSON:\n```json\n" + json.dumps(payload)[:-1] + ",}\n```"
    service = _memory_with_turns()
    result = asyncio.run(CompactionEngine(PalCompactionPolicy()).run(
        _left_snapshot(service), llm_runtime=_ScriptedLLM([generation_result_from_values(text=raw)]),
        memory_service=service))
    assert result.success
    assert len(result.summary_entry.payload["memory_candidates"]) == 1
    assert result.summary_entry.payload["compaction_diagnostics"]
    assert extract_json_object('{"text":"a,}\\n  b",}')["text"] == "a,}\n  b"
    with pytest.raises(ValueError):
        extract_json_object('{"x":1,"x":2}')
    with pytest.raises(ValueError):
        extract_json_object('{"x":1} {"x":2}')


def test_registered_commit_capability_is_reached_only_after_final_action(setup_review):
    import asyncio
    from pal.control.contracts import ControlAction
    from pal.core import MainContext
    from pal.memory.capabilities import register_with_core
    reviews, provider, _ = setup_review
    context = MainContext()
    handle = register_with_core(context, reviews.memory)
    context.execution_runtime.hydrate_module_handle(handle)
    context.execution_runtime.mount_subtree(handle)
    handler = handle.control_action_handlers["memory_candidate_decision"]
    state = stage(reviews)
    async def run():
        accepted = await handler(ControlAction("memory_candidate_decision", "memory",
            target_id=state["batch_id"], route=ROUTE,
            args={"decision": "accept", "candidate_id": "c1", "revision": state["revision"]}))
        assert provider.repository.list_projection_rows() == []
        button = accepted["delivery"].interaction.buttons[0][0]
        args = button.action_args["args"]
        result = await handler(ControlAction("memory_candidate_decision", "memory",
            target_id=state["batch_id"], route=ROUTE, args=args))
        assert result["delivery"].delivery_kind == "interactive_resolve"
        assert len(provider.repository.list_projection_rows()) == 1
    asyncio.run(run())


def test_sequential_handler_edits_only_current_candidate_then_returns_to_it(setup_review):
    import asyncio
    from pal.control.contracts import ControlAction
    from pal.core import MainContext
    from pal.memory.capabilities import register_with_core
    reviews, provider, _ = setup_review
    context = MainContext()
    handle = register_with_core(context, service=reviews.memory)
    context.execution_runtime.mount_subtree(handle)
    handler = handle.control_action_handlers["memory_candidate_decision"]
    state = stage(reviews, candidates=[FACT, {**FACT, "title": "Second"}])

    async def click(button, **extra):
        args = {**button.action_args["args"], **extra}
        result = await handler(ControlAction("memory_candidate_decision", "memory",
            target_id=state["batch_id"], route=ROUTE, args=args))
        return result["delivery"].interaction

    async def run():
        card = reviews.delivery(state, ROUTE).interaction
        fields = await click(card.items[0].buttons[0][2])
        body = next(row[0] for row in fields.buttons if row[0].label == "Body")
        editor = await click(body)
        assert editor.inputs[0].value == TEXT
        edited = TEXT + "# revised\n"
        card = await click(editor.inputs[0].submit, input_values={"value": edited})
        assert len(card.items) == 1 and card.items[0].item_id == "c1"
        assert edited in card.items[0].text
        persisted = reviews.get(state["batch_id"], ROUTE)
        assert persisted["drafts"][0]["decision"] == "pending"
        assert persisted["drafts"][1]["content"]["summary"] == TEXT
        card = await click(card.items[0].buttons[0][0])
        assert len(card.items) == 1 and card.items[0].item_id == "c2"
        final = await click(card.items[0].buttons[0][1])
        assert final.items == () and "Final review" in final.text
        assert final.buttons[0][0].label == "Submit accepted items"
        assert provider.repository.list_projection_rows() == []
        revisited = await click(final.buttons[1][0])
        assert len(revisited.items) == 1 and revisited.items[0].item_id == "c1"
    asyncio.run(run())


@pytest.mark.parametrize("dependencies", [{"node_run_id": "node"}, {}, None])
def test_source_dependency_validation_failure_keeps_authorized_draft(setup_review, dependencies):
    reviews, provider, _ = setup_review
    state = stage(reviews, source={"source_kind": "bunshin", "task_id": "task",
                                  "source_dependencies": dependencies})
    state = decision(reviews, state, "accept", candidate_id="c1")
    state = decision(reviews, state, "submit")
    with pytest.raises(ValueError, match="source"):
        reviews.commit(state["batch_id"])
    with pytest.raises(ValueError, match="source"):
        reviews.commit(state["batch_id"], validate_source=lambda source: False)
    assert not provider.repository.list_projection_rows()
    assert reviews.get(state["batch_id"], ROUTE)["drafts"]
    assert reviews.commit(state["batch_id"], validate_source=lambda source: True)["status"] == "ok"


def test_manager_rejects_stale_or_superseded_proposal_sources():
    from pal.bunshin.manager import BunshinManager
    from pal.bunshin.v2.contracts import AggregateType
    workflow = SimpleNamespace(aggregate_id="workflow", payload={"task_id": "task", "execution_epoch_id": "epoch"})
    node = SimpleNamespace(workflow_id="workflow", state="ACCEPTED", payload={"epoch_id": "epoch"})
    repository = SimpleNamespace(read_snapshot=lambda kind, identity: workflow if kind == AggregateType.WORKFLOW else node)
    manager = SimpleNamespace(v2_service=SimpleNamespace(repository=repository))
    source = {"task_id": "task", "workflow_id": "workflow",
              "source_dependencies": {"epoch_id": "epoch", "node_run_id": "node", "requires_accepted": True}}
    assert BunshinManager._validate_memory_proposal_source(manager, source)
    node.state = "STALE"
    assert not BunshinManager._validate_memory_proposal_source(manager, source)
    node.state = "ACCEPTED"
    workflow.payload["execution_epoch_id"] = "successor"
    assert not BunshinManager._validate_memory_proposal_source(manager, source)


def test_compact_repairs_latest_visible_output_and_prioritizes_source():
    import asyncio
    from pal.core.compaction import CompactionEngine
    from pal.core.pal_compaction import PalCompactionPolicy
    from pal.llm import generation_result_from_values, LLMPreflightAdvice
    from pal.shared import LLMPreflightStatus
    from tests.test_runtime_compaction import _ScriptedLLM, _memory_with_turns, _valid_pal_payload
    service = _memory_with_turns()
    llm = _ScriptedLLM([generation_result_from_values(text="FIRST_INVALID"),
        generation_result_from_values(text="SECOND_INVALID"),
        generation_result_from_values(text=_valid_pal_payload())])
    result = asyncio.run(CompactionEngine(PalCompactionPolicy()).run(_left_snapshot(service),
        llm_runtime=llm, memory_service=service))
    assert result.success
    assert [m.text for m in llm.generate_requests[2].messages if m.semantic_kind == "compaction_failed_output"] == ["SECOND_INVALID"]
    assert "FIRST_INVALID" not in "\n".join(m.text for m in llm.generate_requests[2].messages)
    def preflight(request):
        has_failed_output = any(m.semantic_kind == "compaction_failed_output" for m in request.request.messages)
        return LLMPreflightAdvice(status=LLMPreflightStatus.COMPACT_REQUIRED if has_failed_output else LLMPreflightStatus.READY)
    service = _memory_with_turns()
    llm = _ScriptedLLM([generation_result_from_values(text="INVALID"),
        generation_result_from_values(text=_valid_pal_payload())], preflight=preflight)
    result = asyncio.run(CompactionEngine(PalCompactionPolicy()).run(_left_snapshot(service),
        llm_runtime=llm, memory_service=service))
    assert result.success
    assert not any(m.semantic_kind == "compaction_failed_output" for m in llm.generate_requests[1].messages)
    assert llm.generate_requests[0].messages[-1].text.count("### memory:") == llm.generate_requests[1].messages[-2].text.count("### memory:")

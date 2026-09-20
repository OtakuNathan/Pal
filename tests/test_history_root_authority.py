"""G1 acceptance: minimal two-segment authority (pal-two-segment-v3).

Maps TEST_MATRIX L01-L08 (cut/promote/R-growth), C01-C08 (compact source,
install, failure, arbitration), and X01-X08 (interrupt/reset/stale-run
arbitration) at the owner level, plus the structural partitions these
depend on.  No live provider, no persistence (HANDOFF G1 scope).

Engine-level facts referenced by C04 (validator rejecting schema-legal JSON
that carries tool calls) are covered by the inherited v2 suite; here the
owner-side consequence is pinned: a run that never reaches READY cannot
install anything.
"""

from __future__ import annotations

import unittest
from dataclasses import replace

from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory.history_root import (
    CandidateConflict,
    CompactLaneBusy,
    CompactPhase,
    FrozenLeftDuringCompact,
    HistoryRoot,
    HistoryRootError,
    LeftChangedSinceCapture,
    NoBeneficialCompaction,
    StaleProducer,
    StaleRun,
    SUMMARY_TURN_ID,
    TerminalClosed,
    default_summary_builder,
)
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from pal.shared.tool_protocol import ToolResultIR, new_tool_call


def _visible_text(messages) -> str:
    """All user-visible content: text parts and tool result payloads."""

    chunks: list[str] = []
    for message in messages:
        for part in message.parts:
            if isinstance(part, TextPartIR):
                chunks.append(part.text)
            elif isinstance(part, ToolResultIR):
                chunks.append(part.content)
    return " ".join(chunks)


def _user(text: str, message_id: str = "") -> LLMMessageIR:
    return LLMMessageIR(
        role=MessageRole.USER,
        parts=(TextPartIR(text),),
        semantic_kind="user_request",
        **({"message_id": message_id} if message_id else {}),
    )


def _assistant(text: str, *calls, message_id: str = "") -> LLMMessageIR:
    return LLMMessageIR(
        role=MessageRole.ASSISTANT,
        parts=(TextPartIR(text), *calls),
        semantic_kind="assistant_reply",
        **({"message_id": message_id} if message_id else {}),
    )


def _seed_turn(text: str = "SEED0 prior summary") -> L1TurnIR:
    return L1TurnIR(
        turn_id=SUMMARY_TURN_ID,
        messages=(
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(TextPartIR(text),),
                semantic_kind="runtime_context_summary",
                message_id="seed-message",
            ),
        ),
        state=L1TurnState.SETTLED,
    )


def _settled_turn(mark: str, turn_id: str = "") -> L1TurnIR:
    return L1TurnIR(
        turn_id=turn_id or f"settled-{mark}",
        messages=(_user(f"{mark} request"), _assistant(f"{mark} reply")),
        state=L1TurnState.SETTLED,
    )


def _result(call_id: str, text: str = "ok") -> ToolResultIR:
    return ToolResultIR(call_id=call_id, name="aggregate", content=text)


class _Candidate:
    """Minimal L2Entry-like object for the default summary builder."""

    def __init__(self, text: str) -> None:
        self.summary = text
        self.rendered = text
        self.payload = {"summary": {"summary": text}}


class _Base(unittest.TestCase):
    def make_root(self) -> HistoryRoot:
        root = HistoryRoot()
        root.store.append(_seed_turn())
        root.store.append(_settled_turn("s1"))
        root.begin_right_turn("task-T", user_text="Q_ORIGINAL: implement the feature")
        root.stream_right_assistant(
            "task-T",
            _assistant(
                "A_DECISION: start with file a.cpp",
                new_tool_call(call_id="call-A", name="read_file", arguments={"file": "a.cpp"}),
                message_id="assistant-1",
            ),
        )
        return root

    def root_with_promoted_left(self) -> HistoryRoot:
        root = self.make_root()
        root.append_right_tool_result(
            root.producer_token("task-T"), _result("call-A", "RESULT_A"))
        root.settle_right_turn("task-T")
        cut = root.promote()
        self.assertEqual(cut.turn_count, 3)  # seed + s1 + settled task-T
        return root

    def promoted_left_with_active_tail(self) -> HistoryRoot:
        """Seed + s1 promoted; task-T stays ACTIVE in R with a pending call."""

        root = self.make_root()
        cut = root.promote()
        self.assertEqual(cut.turn_count, 2)
        return root

    def snapshot_ids(self, snapshot) -> set[str]:
        return set(snapshot.message_ids())

    def combined_message_ids(self, root: HistoryRoot) -> tuple[str, ...]:
        return tuple(m.message_id for t in root.all_turns() for m in t.messages)

    def left_stamp(self, root: HistoryRoot) -> str:
        return root._left_stamp()


class PartitionTests(_Base):
    """A03/I02 structural substrate for every L/C/X case."""

    def test_left_plus_right_partitions_store_exactly(self):
        root = HistoryRoot()
        root.store.append(_seed_turn())
        root.store.append(_settled_turn("s1"))
        root.store.append(_settled_turn("s2"))
        cut = root.promote()
        self.assertEqual(cut.turn_count, 3)
        left_ids = [m.message_id for m in root.left_messages()]
        right_ids = [m.message_id for m in root.right_messages()]
        all_ids = list(self.combined_message_ids(root))
        self.assertEqual(left_ids + right_ids, all_ids)
        self.assertFalse(set(left_ids) & set(right_ids))

    def test_intra_cut_keeps_partition_on_active_turn(self):
        root = self.make_root()
        root.append_right_tool_result(
            root.producer_token("task-T"), _result("call-A", "RESULT_A"))
        # task-T is ACTIVE with a closed user-prefix only (assistant call
        # block is closed as soon as its single result arrived).
        root.promote(include_active=True)
        left_ids = [m.message_id for m in root.left_messages()]
        right_ids = [m.message_id for m in root.right_messages()]
        self.assertEqual(left_ids + right_ids, list(self.combined_message_ids(root)))
        self.assertFalse(set(left_ids) & set(right_ids))

    def test_active_turn_closed_round_prefix_promotes(self):
        root = self.make_root()
        root.append_right_tool_result(
            root.producer_token("task-T"), _result("call-A", "RESULT_A"))
        cut = root.promote(include_active=True)
        self.assertEqual(cut.turn_count, 2)
        self.assertGreater(cut.intra_messages, 0)
        semantic = [m.semantic_kind for m in root.left_messages()]
        self.assertIn("user_request", semantic)
        # The whole group (assistant text + call + result) is closed now.
        right_roles = [m.role for m in root.right_messages()]
        self.assertEqual(right_roles, [])


class LPromoteTests(_Base):
    """L01-L08: cut legality, promote discipline, R growth."""

    def test_L01_open_group_stays_whole_in_right(self):
        root = self.make_root()
        # One aggregate assistant message carrying two sub-calls; the first
        # sub-result arrives, call-B is still open.
        root.stream_right_assistant(
            "task-T",
            _assistant(
                "A_DECISION",
                new_tool_call(call_id="call-A", name="read_file", arguments={"file": "a.cpp"}),
                new_tool_call(call_id="call-B", name="read_file", arguments={"file": "b.cpp"}),
                message_id="assistant-1",
            ),
        )
        root.append_right_tool_result(
            root.producer_token("task-T"), _result("call-A", "RESULT_A"))
        cut = root.promote()  # settled prefix (seed + s1) moves
        self.assertEqual(cut.turn_count, 2)
        frozen = root.cut
        root.promote()  # the active turn never moves by default
        self.assertEqual(root.cut, frozen)
        root.promote(include_active=True)
        # Longest closed prefix of task-T: the trailing user message is the
        # pending work tail awaiting its response (PLAN §3.3 keep the newest
        # work tail), so the whole OPEN group [user, assistant(call-A,
        # call-B open), result-A] stays in R — the cut does not split it.
        left_kinds = [m.semantic_kind for m in root.left_messages()]
        self.assertEqual(left_kinds,
                         ["runtime_context_summary", "user_request",
                          "assistant_reply"])
        right_roles = [m.role for m in root.right_messages()]
        self.assertEqual(right_roles,
                         [MessageRole.USER, MessageRole.ASSISTANT, MessageRole.TOOL])
        right_text = _visible_text(root.right_messages())
        self.assertIn("A_DECISION", right_text)
        call_ids = [
            p.call_id for m in root.right_messages() for p in m.parts
            if hasattr(p, "call_id")
        ]
        self.assertIn("call-A", call_ids)
        self.assertIn("call-B", call_ids)
        self.assertIn("RESULT_A", right_text)

    def test_L02_settle_does_not_auto_promote(self):
        root = self.make_root()
        root.append_right_tool_result(
            root.producer_token("task-T"), _result("call-A", "RESULT_A"))
        root.settle_right_turn("task-T")
        self.assertEqual(root.cut.turn_count, 0)
        self.assertEqual(len(root.right_turns()), 3)
        cut = root.promote()
        self.assertEqual(cut.turn_count, 3)

    def test_L03_compact_original_L_latest_R_not_moved(self):
        root = self.root_with_promoted_left()
        # A big fresh active turn drives the normal view over budget.
        root.begin_right_turn("task-NEW", user_text="NEW_BULK_REQUEST")
        root.promote()  # cannot move task-NEW: ACTIVE
        self.assertEqual(root.cut.turn_count, 3)
        snapshot = root.begin_compact("run-L03", reason="budget")
        snapshot_text = _visible_text(
            m for t in snapshot.turns for m in t.messages)
        self.assertNotIn("NEW_BULK_REQUEST", snapshot_text)
        self.assertIn("Q_ORIGINAL", snapshot_text)

    def test_L04_promote_preserves_content_and_order(self):
        root = HistoryRoot()
        root.store.append(_seed_turn())
        for mark in ("s1", "s2", "s3"):
            root.store.append(_settled_turn(mark))
        before = list(self.combined_message_ids(root))
        cut = root.promote()
        self.assertEqual(cut.turn_count, 4)
        left_ids = [m.message_id for m in root.left_messages()]
        self.assertEqual(left_ids, before)
        self.assertEqual(root.right_messages(), ())

    def test_L05_result_arriving_during_compact_lands_in_R_once(self):
        root = self.root_with_promoted_left()
        root.begin_right_turn("task-2", user_text="second task")
        root.stream_right_assistant(
            "task-2",
            _assistant("working",
                       new_tool_call(call_id="call-2", name="read_file", arguments={}),
                       message_id="assistant-2t"),
        )
        snapshot = root.begin_compact("run-L05", reason="auto")
        token = root.producer_token("task-2")
        root.append_right_tool_result(token, _result("call-2", "RESULT_2"))
        # L frozen while the run is live; the new fact is only in R.
        self.assertEqual(root._left_stamp(), snapshot.stamp)
        root.mark_ready("run-L05", _Candidate("summary-L05"), candidate_id="c1")
        root.commit("run-L05")
        combined = list(self.combined_message_ids(root))
        self.assertEqual(combined.count("assistant-2t"), 1)
        self.assertIn("RESULT_2", _visible_text(root.right_messages()))
        left = root.left_turns()
        self.assertEqual(len(left), 1)
        self.assertTrue(all(t.turn_id == SUMMARY_TURN_ID for t in left))

    def test_L06_result_and_commit_commute(self):
        def build():
            root = self.root_with_promoted_left()
            root.begin_right_turn(
                "task-2", user_message=_user("second task", message_id="task2-user"))
            root.stream_right_assistant(
                "task-2",
                _assistant("working",
                           new_tool_call(call_id="call-2", name="read_file", arguments={}),
                           message_id="assistant-2t"),
            )
            return root

        # result -> commit
        a = build()
        a.begin_compact("run", reason="auto")
        a.append_right_tool_result(a.producer_token("task-2"), _result("call-2", "R2"))
        a.mark_ready("run", _Candidate("S"), candidate_id="c")
        a.commit("run")
        # commit -> result
        b = build()
        b.begin_compact("run", reason="auto")
        b.mark_ready("run", _Candidate("S"), candidate_id="c")
        b.commit("run")
        b.append_right_tool_result(b.producer_token("task-2"), _result("call-2", "R2"))

        def normalize(messages):
            return [
                (m.role, m.semantic_kind,
                 tuple(getattr(p, "call_id", getattr(p, "text", "")) for p in m.parts))
                for m in messages
            ]

        self.assertEqual(normalize(a.right_messages()), normalize(b.right_messages()))
        self.assertEqual([t.turn_id for t in a.left_turns()],
                         [t.turn_id for t in b.left_turns()])
        self.assertEqual(normalize(a.all_turns() and a.left_messages() + a.right_messages()),
                         normalize(b.left_messages() + b.right_messages()))

    def test_L07_cut_frozen_while_compact_live(self):
        root = self.root_with_promoted_left()
        root.begin_right_turn("task-2", user_text="second")
        root.settle_right_turn("task-2")
        root.begin_compact("run-L07", reason="auto")
        with self.assertRaises(FrozenLeftDuringCompact) as ctx:
            root.promote()
        self.assertEqual(ctx.exception.detail["run_id"], "run-L07")
        self.assertEqual(root.cut.turn_count, 3)

    def test_L08_minimal_left_refuses_recompaction(self):
        root = HistoryRoot()  # empty left
        with self.assertRaises(NoBeneficialCompaction) as ctx:
            root.begin_compact("run-empty", reason="auto")
        self.assertEqual(ctx.exception.detail["run_id"], "run-empty")
        root = self.root_with_promoted_left()
        root.begin_compact("run-1", reason="auto")
        root.mark_ready("run-1", _Candidate("S1"), candidate_id="c1")
        root.commit("run-1")
        self.assertEqual(len(root.left_turns()), 1)  # minimal seed
        with self.assertRaises(NoBeneficialCompaction):
            root.begin_compact("run-2", reason="auto")
        # R facts survive every refusal.
        root.begin_right_turn("later", user_text="kept")
        self.assertIn("kept", " ".join(p.text for m in root.right_messages()
                                       for p in m.parts if isinstance(p, TextPartIR)))


class CCompactTests(_Base):
    """C01-C08: source selection, install, failure, one-winner."""

    def test_C01_snapshot_reads_left_only(self):
        root = HistoryRoot()
        seed = _seed_turn()
        marked = replace(_settled_turn("LEFTMARK"), messages=(
            _user("LEFT_SENTINEL user"), _assistant("LEFT_SENTINEL reply"),
        ))
        root.store.append(seed)
        root.store.append(marked)
        root.begin_right_turn("task-T", user_text="RIGHT_SENTINEL task")
        root.promote()
        snapshot = root.begin_compact("run-C01", reason="manual")
        text = " ".join(p.text for t in snapshot.turns for m in t.messages
                        for p in m.parts if isinstance(p, TextPartIR))
        self.assertEqual(text.count("SEED0"), 1)
        self.assertEqual(text.count("LEFT_SENTINEL"), 2)
        self.assertNotIn("RIGHT_SENTINEL", text)
        self.assertTrue(all(t.turn_id != "task-T" for t in snapshot.turns))

    def test_C02_install_replaces_only_left(self):
        root = self.root_with_promoted_left()
        right_before = [replace(t) for t in root.right_turns()]
        root.begin_right_turn("task-keep", user_text="keep me",
                              metadata={"observation_coverage": {"ns": {"states": {"k": 1}}}})
        root.promote()  # task-keep is ACTIVE; nothing new moves
        snapshot = root.begin_compact("run-C02", reason="auto")
        root.mark_ready("run-C02", _Candidate("summary-C02"), candidate_id="c1")
        outcome = root.commit("run-C02")
        self.assertEqual(outcome.status, "committed")
        left = root.left_turns()
        self.assertEqual([t.turn_id for t in left], [SUMMARY_TURN_ID])
        right_after = root.right_turns()
        kept = next(t for t in right_after if t.turn_id == "task-keep")
        self.assertIn("keep me", kept.messages[0].text)
        self.assertEqual(kept.metadata.get("observation_coverage"),
                         {"ns": {"states": {"k": 1}}})
        self.assertEqual(root.cut.revision, snapshot.cut.revision + 1)
        # The pre-compact settled turn is gone (it is inside the seed now).
        self.assertFalse(any(t.turn_id == "settled-s1" for t in root.all_turns()))

    def test_C03_failure_keeps_current_R_additions(self):
        root = self.root_with_promoted_left()
        root.begin_right_turn("task-2", user_text="second")
        root.stream_right_assistant(
            "task-2",
            _assistant("working", new_tool_call(call_id="call-9", name="x", arguments={}),
                       message_id="assistant-9"))
        stamp_before = self.left_stamp(root)
        root.begin_compact("run-C03", reason="auto")
        root.append_right_tool_result(root.producer_token("task-2"),
                                      _result("call-9", "RESULT_9"))
        outcome = root.fail("run-C03", reason="format error")
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(self.left_stamp(root), stamp_before)
        self.assertIn("RESULT_9", _visible_text(root.right_messages()))

    def test_C04_validator_rejection_never_installs(self):
        root = self.root_with_promoted_left()
        run_id = "run-C04"
        snapshot = root.begin_compact(run_id, reason="auto")
        # The validator (engine-side, inherited v2 coverage) rejects the
        # schema-legal JSON carrying a tool call before READY is reached.
        class Rejected(Exception):
            pass

        def validator(raw: str):
            raise Rejected("valid JSON with toolcall is not a summary")

        with self.assertRaises(Rejected):
            validator('{"summary": "...", "tool_calls": []}')
        root.fail(run_id, reason="validator rejected candidate")
        self.assertEqual(self.left_stamp(root), snapshot.stamp)
        with self.assertRaises(TerminalClosed):
            root.mark_ready(run_id, _Candidate("late"), candidate_id="late")

    def test_C05_construction_failure_does_not_half_publish(self):
        root = self.root_with_promoted_left()
        root.begin_compact("run-C05", reason="auto")
        root.mark_ready("run-C05", _Candidate("S"), candidate_id="c1")
        turns_before = list(root.all_turns())
        cut_before = root.cut

        def poison(candidate):
            raise ValueError("construction exploded")

        with self.assertRaises(ValueError):
            root.commit("run-C05", build_summary_turn=poison)
        self.assertEqual(list(root.all_turns()), turns_before)
        self.assertEqual(root.cut, cut_before)
        # The run stays READY: a corrected builder can still commit.
        outcome = root.commit("run-C05")
        self.assertEqual(outcome.status, "committed")

    def test_C06_duplicate_completion_installs_once(self):
        root = self.root_with_promoted_left()
        root.begin_compact("run-C06", reason="auto")
        root.mark_ready("run-C06", _Candidate("S"), candidate_id="c1")
        first = root.commit("run-C06")
        revision_after_first = root.left_revision
        second = root.commit("run-C06")
        self.assertTrue(second.replayed)
        self.assertEqual(second.status, "committed")
        self.assertEqual(root.left_revision, revision_after_first)
        summary_turns = [t for t in root.all_turns() if t.turn_id == SUMMARY_TURN_ID]
        self.assertEqual(len(summary_turns), 1)
        self.assertEqual(second.left_revision, first.left_revision)

    def test_C07_conflicting_candidate_rejected(self):
        root = self.root_with_promoted_left()
        root.begin_compact("run-C07", reason="auto")
        root.mark_ready("run-C07", _Candidate("A"), candidate_id="cand-A")
        with self.assertRaises(CandidateConflict) as ctx:
            root.mark_ready("run-C07", _Candidate("B"), candidate_id="cand-B")
        self.assertEqual(ctx.exception.detail["accepted_candidate_id"], "cand-A")
        root.commit("run-C07")
        seed_text = root.left_turns()[0].messages[0].text
        self.assertEqual(seed_text, "A")

    def test_C08_full_left_source_is_not_truncated(self):
        root = HistoryRoot()
        long_middle = _user("MIDDLE_CONSTRAINT " + "payload " * 200)
        turn = L1TurnIR(
            turn_id="settled-long",
            messages=(_user("head"), long_middle, _assistant("tail")),
            state=L1TurnState.SETTLED,
        )
        root.store.append(_seed_turn())
        root.store.append(turn)
        root.promote()
        snapshot = root.begin_compact("run-C08", reason="manual")
        source_ids = list(snapshot.message_ids())
        self.assertIn(long_middle.message_id, source_ids)
        self.assertEqual(source_ids[0], "seed-message")
        self.assertEqual(len(source_ids), 4)
        self.assertEqual(snapshot.stamp, self.left_stamp(root))


class XArbitrationTests(_Base):
    """X01-X08: interrupt, stale runs, reset, deadline."""

    def _auto_run(self, root: HistoryRoot, run_id: str = "run-X",
                  turn_id: str = "task-T"):
        root.begin_compact(run_id, reason="auto", parent_turn_id=turn_id)

    def test_X01_cancel_wins_then_late_summary_rejected(self):
        root = self.promoted_left_with_active_tail()
        stamp = self.left_stamp(root)
        self._auto_run(root)
        verdict = root.interrupt_compaction_for_turn("task-T")
        self.assertEqual(verdict, "cancelled")
        with self.assertRaises(TerminalClosed) as ctx:
            root.mark_ready("run-X", _Candidate("late summary"))
        self.assertEqual(ctx.exception.winner, "cancelled")
        self.assertEqual(self.left_stamp(root), stamp)
        root.interrupt_right_turn("task-T", reason="user interrupt")
        turn = root.store.get("task-T")
        self.assertEqual(turn.state, L1TurnState.INTERRUPTED)

    def test_X02_commit_wins_interrupt_keeps_new_left(self):
        root = self.root_with_promoted_left()
        self._auto_run(root)
        root.mark_ready("run-X", _Candidate("S"), candidate_id="c")
        root.commit("run-X")
        stamp_after = self.left_stamp(root)
        verdict = root.interrupt_compaction_for_turn("task-T")
        self.assertEqual(verdict, "committed")
        self.assertEqual(self.left_stamp(root), stamp_after)
        self.assertEqual(len(root.left_turns()), 1)

    def test_X03_idle_manual_is_not_a_turn(self):
        root = self.root_with_promoted_left()
        root.begin_compact("run-idle", reason="manual", parent_turn_id="")
        verdict = root.interrupt_compaction_for_turn("task-T")
        self.assertEqual(verdict, "no_active_turn")
        self.assertEqual(root.active_run.run_id, "run-idle")
        root.mark_ready("run-idle", _Candidate("S"), candidate_id="c")
        outcome = root.commit("run-idle")
        self.assertEqual(outcome.status, "committed")

    def test_X04_old_run_events_cannot_touch_new_run(self):
        root = self.root_with_promoted_left()
        root.begin_compact("run-A", reason="auto")
        root.fail("run-A", reason="error")
        root.begin_compact("run-B", reason="auto")
        self.assertEqual(root.fail("run-A", reason="late finally").status, "failed")
        self.assertTrue(root.fail("run-A").replayed)
        with self.assertRaises(TerminalClosed):
            root.commit("run-A")
        self.assertEqual(root.active_run.run_id, "run-B")
        root.mark_ready("run-B", _Candidate("S"), candidate_id="c")
        self.assertEqual(root.commit("run-B").status, "committed")

    def test_X05_reset_fences_old_incarnation_producers(self):
        root = self.root_with_promoted_left()
        root.begin_right_turn("task-2", user_text="pending work")
        root.stream_right_assistant(
            "task-2",
            _assistant("working", new_tool_call(call_id="call-5", name="x", arguments={}),
                       message_id="assistant-5"))
        token = root.producer_token("task-2")
        self._auto_run(root)
        root.reset()
        self.assertNotEqual(token.incarnation, root.incarnation)
        with self.assertRaises(StaleProducer) as ctx:
            root.append_right_tool_result(token, _result("call-5", "late"))
        self.assertEqual(ctx.exception.detail["turn_id"], "task-2")
        self.assertEqual(root.all_turns(), ())
        self.assertEqual(root.left_turns(), ())

    def test_X06_left_rebase_does_not_fence_producers(self):
        root = self.root_with_promoted_left()
        root.begin_right_turn("task-2", user_text="second")
        root.stream_right_assistant(
            "task-2",
            _assistant("working", new_tool_call(call_id="call-6", name="x", arguments={}),
                       message_id="assistant-6"))
        token = root.producer_token("task-2")
        revision_before = root.left_revision
        root.begin_compact("run-X06", reason="auto")
        root.mark_ready("run-X06", _Candidate("S"), candidate_id="c")
        root.commit("run-X06")
        self.assertGreater(root.left_revision, revision_before)
        updated = root.append_right_tool_result(token, _result("call-6", "RESULT_6"))
        self.assertIn("RESULT_6", _visible_text(updated.messages))

    def test_X07_interrupt_never_touches_left(self):
        root = self.promoted_left_with_active_tail()
        stamp = self.left_stamp(root)
        root.interrupt_right_turn("task-T", reason="user interrupt")
        self.assertEqual(self.left_stamp(root), stamp)
        turn = root.store.get("task-T")
        self.assertEqual(turn.state, L1TurnState.INTERRUPTED)
        # No synthetic success receipts: nothing was fabricated for calls
        # whose results never arrived.
        result_ids = [
            p.call_id for m in turn.messages for p in m.parts
            if hasattr(p, "call_id") and isinstance(p, ToolResultIR)
        ]
        self.assertEqual(result_ids, [])

    def test_X08_deadline_failure_beats_late_network_success(self):
        root = self.root_with_promoted_left()
        root.begin_compact("run-X08", reason="auto", deadline_at=100.0)
        outcome = root.expire_deadline(200.0)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.status, "failed")
        stamp = self.left_stamp(root)
        root.mark_ready  # noqa: B018 - late network delivery attempts READY...
        with self.assertRaises(TerminalClosed):
            root.mark_ready("run-X08", _Candidate("late"))
        with self.assertRaises(TerminalClosed):
            root.commit("run-X08")
        self.assertEqual(self.left_stamp(root), stamp)


class InstallPhysicalizationTests(_Base):
    """Install with an intra-turn cut keeps the turn's home and identity."""

    def test_intra_install_keeps_active_turn_home(self):
        root = self.make_root()
        root.append_right_tool_result(
            root.producer_token("task-T"), _result("call-A", "RESULT_A"))
        cut = root.promote(include_active=True)
        self.assertGreater(cut.intra_messages, 0)
        root.begin_compact("run-intra", reason="auto")
        root.mark_ready("run-intra", _Candidate("S"), candidate_id="c")
        root.commit("run-intra")
        # task-T keeps its identity with an empty remainder and can host a
        # new round (I15 turn continuity).
        remainder = root.store.get("task-T")
        self.assertIsNotNone(remainder)
        self.assertEqual(remainder.state, L1TurnState.ACTIVE)
        root.stream_right_assistant(
            "task-T", _assistant("next round", message_id="assistant-next"))
        self.assertIn("next round",
                      " ".join(p.text for m in root.right_messages()
                               for p in m.parts if isinstance(p, TextPartIR)))

    def test_commit_with_live_round_folds_streamed_content(self):
        root = self.make_root()
        root.promote()  # seed + s1 move; task-T active with a pending call
        root.begin_compact("run-open", reason="auto")
        root.mark_ready("run-open", _Candidate("S"), candidate_id="c")
        # The authorized tool group is still mid-flight (stream finished,
        # result outstanding): commit must not drop or block it (F04).
        outcome = root.commit("run-open")
        self.assertEqual(outcome.status, "committed")
        self.assertIn("A_DECISION", _visible_text(root.right_messages()))
        updated = root.append_right_tool_result(
            root.producer_token("task-T"), _result("call-A", "RESULT_A"))
        self.assertIn("RESULT_A", _visible_text(updated.messages))
        combined = list(self.combined_message_ids(root))
        self.assertEqual(combined.count("assistant-1"), 1)
        # A next stream fragment re-anchors a fresh round on the new record.
        root.stream_right_assistant(
            "task-T", _assistant("next round", message_id="assistant-next"))
        self.assertIn("next round", _visible_text(root.right_messages()))

    def test_second_compact_while_live_refused(self):
        root = self.root_with_promoted_left()
        root.begin_compact("run-1", reason="auto")
        with self.assertRaises(CompactLaneBusy) as ctx:
            root.begin_compact("run-2", reason="auto")
        self.assertEqual(ctx.exception.detail["active_run_id"], "run-1")

    def test_run_ids_are_never_recycled(self):
        root = self.root_with_promoted_left()
        root.begin_compact("run-1", reason="auto")
        root.cancel("run-1")
        with self.assertRaises(HistoryRootError):
            root.begin_compact("run-1", reason="auto")

    def test_stale_run_id_rejected(self):
        root = self.root_with_promoted_left()
        with self.assertRaises(StaleRun):
            root.mark_ready("never-started", _Candidate("S"))

    def test_default_summary_builder_rejects_empty(self):
        with self.assertRaises(ValueError):
            default_summary_builder(type("Empty", (), {"summary": "", "rendered": ""})())


if __name__ == "__main__":
    unittest.main()

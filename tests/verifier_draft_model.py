"""Small explicit-state design experiments; NOT a TLA parser or TLC result.

The four scenarios restrict independent axes to keep Python exploration small.
They exhaust their own finite spaces, not the full TLA model or production code.
No provider, running service, filesystem corpus, or live database is accessed.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace


FINDINGS = ("absent", "pending-blocking", "completed-blocking",
            "pending-advisory", "completed-advisory")
CASES = ("absent", "case-a", "case-b")
HISTORY = ("inherited:finding-0:case-0", "submitted:finding-1:case-1")


@dataclass(frozen=True)
class Evidence:
    generation: int = 1
    corpus: int = 0
    case_version: int = 0
    result: str = "pass"
    history_passed: bool = True


@dataclass(frozen=True)
class Snapshot:
    finding: str
    case: str
    finding_version: int
    verification_version: int
    generation: int
    corpus: int
    evidence: Evidence


@dataclass(frozen=True)
class Request:
    token: tuple
    snapshot: Snapshot
    verdict: str


@dataclass(frozen=True)
class State:
    finding: str = "absent"
    case: str = "case-a"
    finding_version: int = 0
    verification_version: int = 0
    generation: int = 1
    fence: int = 1
    corpus: int = 0
    evidence: Evidence = Evidence(0, 0, 0, "none", False)
    accepted_corpus: int = 0
    history: tuple = HISTORY
    request: Request | None = None
    receipt: Request | None = None
    phase: str = "none"
    projection: str = "active"
    operation: tuple | None = None
    authorized: bool = True


def authority(s):
    return ("verifier", "assignment-0", s.generation, s.fence)


def snapshot(s):
    return Snapshot(s.finding, s.case, s.finding_version, s.verification_version,
                    s.generation, s.corpus, s.evidence)


def fresh(p):
    return (p.evidence.generation == p.generation
            and p.evidence.corpus == p.corpus
            and p.evidence.case_version == p.verification_version
            and p.evidence.result == "pass" and p.evidence.history_passed)


def recover_source_as_current(projection, phase, owned, same_input, fault="none"):
    return (owned and same_input and projection == "active"
            and (phase == "none" or fault == "receipt-blind-inheritance"))


class Model:
    def __init__(self, scenario="finding", fault="none"):
        assert scenario in {"finding", "case", "authority", "history"}
        self.scenario, self.fault = scenario, fault

    def tokens(self, s):
        current = authority(s)
        if self.scenario != "authority":
            return (current,)
        return (current, ("other-role", *current[1:]),
                (current[0], "other-assignment", *current[2:]),
                (*current[:2], 0, current[3]), (*current[:3], 0))

    def editable(self, s):
        return s.projection == "active" and (s.receipt is None or self.fault == "receipt-blind")

    def authorized(self, s, token):
        return token == authority(s) or self.fault == "stale-authority"

    def edit_finding(self, s, value, token=None, expected=None, target="current"):
        token = authority(s) if token is None else token
        expected = s.finding_version if expected is None else expected
        if not (self.editable(s) and self.authorized(s, token)
                and expected == s.finding_version < 2 and value != s.finding
                and (target == "current" or self.fault == "history-rewrite")):
            return None
        if self.fault == "completed-locked" and s.finding.startswith("completed"):
            return None
        return replace(s, finding=value, finding_version=s.finding_version + 1,
                       history=s.history if target == "current" else (),
                       authorized=token == authority(s),
                       operation=s.operation or ("finding", value, token))

    def edit_case(self, s, value, expected=None):
        expected = s.verification_version if expected is None else expected
        if not (self.editable(s) and expected == s.verification_version < 2 and value != s.case):
            return None
        return replace(s, case=value, verification_version=expected + 1,
                       operation=s.operation or ("case", value, authority(s)))

    def record(self, s, result="pass", covered=True):
        if not (self.editable(s) and s.case != "absent" and s.verification_version < 2):
            return None
        version = s.verification_version + 1
        return replace(s, verification_version=version,
                       evidence=Evidence(s.generation, s.corpus, version, result, covered))

    def replay_enabled(self, s, value):
        return (s.operation is not None and self.editable(s)
                and self.authorized(s, s.operation[2])
                and (value == s.operation[1] or self.fault == "conflicting-replay"))

    def commit(self, s):
        r = s.request
        if r is None or s.receipt is not None or not self.authorized(s, r.token):
            return None
        p = r.snapshot
        versions = (p.finding_version, p.verification_version, p.corpus)
        if versions != (s.finding_version, s.verification_version, s.corpus) and self.fault != "submit-without-cas":
            return None
        if p.finding != s.finding and self.fault != "omit-finding":
            return None
        if r.verdict == "pass" and not (
            p.finding in {"absent", "pending-advisory", "completed-advisory"} and p.case != "absent"
            and (fresh(p) or self.fault == "stale-evidence")
        ):
            return None
        return replace(s, receipt=r, phase="pending", accepted_corpus=s.corpus,
                       authorized=r.token == authority(s))

    def successors(self, s):
        if self.scenario in {"finding", "authority", "history"}:
            for token in self.tokens(s):
                for value in FINDINGS:
                    yield f"edit finding -> {value}", self.edit_finding(s, value, token)
            if self.scenario == "history":
                yield "edit inherited finding", self.edit_finding(s, "completed-blocking", target="inherited")
        if self.scenario == "case":
            for value in CASES:
                yield f"edit case -> {value}", self.edit_case(s, value)
            for result, covered in (("pass", True), ("fail", True), ("pass", False)):
                yield f"record {result}, history covered={covered}", self.record(s, result, covered)
            if s.corpus < 1:
                yield "edit physical corpus", replace(s, corpus=s.corpus + 1)
        if self.scenario != "case" and not fresh(snapshot(s)):
            yield "record fresh current evidence", self.record(s)
        if s.receipt is None:
            if self.scenario == "authority":
                if s.generation == 1:
                    yield "replace generation", replace(s, generation=2, operation=None)
                if s.fence == 1:
                    yield "replace fence", replace(s, fence=2, operation=None)
            if s.request is None:
                for token in self.tokens(s):
                    if not self.authorized(s, token):
                        continue
                    for verdict in ("pass", "fail"):
                        for finding in {s.finding, "absent"}:
                            proposed = replace(snapshot(s), finding=finding)
                            yield f"prepare {verdict}, finding={finding}, token={token}", replace(
                                s, request=Request(token, proposed, verdict))
            else:
                yield "discard prepared submit", replace(s, request=None)
                yield "commit submit", self.commit(s)
        else:
            if s.projection == "active":
                yield "project submitted", replace(s, projection="submitted")
            if s.phase == "pending":
                yield "accept receipt", replace(s, phase="accepted")
            if self.fault == "replay-rewrite":
                changed = "completed-blocking" if s.receipt.snapshot.finding == "absent" else "absent"
                yield "rewrite receipt on replay", replace(s, receipt=replace(
                    s.receipt, snapshot=replace(s.receipt.snapshot, finding=changed)))

    def violations(self, s):
        if any(recover_source_as_current(projection, phase, True, True, self.fault)
               for projection in ("active", "submitted") for phase in ("pending", "accepted")):
            yield "SourceReceiptsFreezeInheritance"
        if s.history != HISTORY:
            yield "HistoryPreserved"
        if not s.authorized:
            yield "MutationsUseCurrentAuthority"
        if s.receipt:
            p = s.receipt.snapshot
            if replace(snapshot(s), corpus=p.corpus) != p:
                yield "ReceiptFreezesBothDrafts"
            if replace(snapshot(s), corpus=p.corpus) != p or p.corpus != s.accepted_corpus:
                yield "AtomicSubmissionSnapshot"
            if s.receipt.verdict == "pass" and not (
                p.finding in {"absent", "pending-advisory", "completed-advisory"}
                and p.case != "absent" and fresh(p)
            ):
                yield "PassRequiresCurrentEvidence"
        if s.receipt is None and s.projection == "active":
            if s.finding_version < 2 and any(self.edit_finding(s, v) is None for v in FINDINGS if v != s.finding):
                yield "DraftAllowsFindingCRUD"
            if s.verification_version < 2 and any(self.edit_case(s, v) is None for v in CASES if v != s.case):
                yield "DraftAllowsCaseCRUD"
        if self.replay_enabled(s, "conflicting-value"):
            yield "MutationReplayRejectsConflicts"


def explore(model):
    """Exhaust one declared scenario; return shortest counterexamples by BFS."""
    initial = State()
    parents = {initial: None}
    pending = deque([initial])
    edges, witnesses = 0, set()
    while pending:
        state = pending.popleft()
        violations = tuple(model.violations(state))
        if violations:
            trace = []
            cursor = state
            while parents[cursor] is not None:
                previous, action = parents[cursor]
                trace.append(action)
                cursor = previous
            return {"kind": "bounded_python_counterexample", "states": len(parents),
                    "invariants": violations, "trace": list(reversed(trace))}
        if state.receipt and state.phase == "pending" and state.projection == "active":
            witnesses.add("receipt_freezes_active_projection")
        if (state.phase == "accepted" and state.receipt.verdict == "pass"
                and state.finding == "absent" and state.finding_version == 2):
            witnesses.add("withdraw_then_pass_with_history")
        if (state.receipt and state.receipt.verdict == "pass"
                and state.corpus != state.receipt.snapshot.corpus):
            witnesses.add("corpus_changes_without_rewriting_sealed_pass")
        for action, after in model.successors(state):
            if after is None:
                continue
            edges += 1
            if after not in parents:
                parents[after] = (state, action)
                pending.append(after)
    return {"kind": "bounded_python_pass", "states": len(parents), "edges": edges,
            "witnesses": sorted(witnesses)}

#!/usr/bin/env python3
"""Bounded Python explicit-state exploration, NOT TLC or TLA parsing.

Mirror of DependencyRepairCohort.tla with five nodes, two immutable packets,
one optional manager crash and one repair wave. No runtime/repository imports.
"""
from dataclasses import dataclass, replace, asdict
from collections import deque, defaultdict

NAMES = ('P', 'Q', 'C', 'D', 'X')
P, Q, C, D, X = range(5)
PC, PD = range(2)
ALL = (1 << 5) - 1
SOURCES = (C, D)

def bit(n): return 1 << n
def members(mask): return [n for n in range(5) if mask & bit(n)]
def packet_members(mask): return [k for k in range(2) if mask & bit(k)]
def setat(seq, i, value): return seq[:i] + (value,) + seq[i + 1:]

def initial():
    return State(('Accepted', 'Accepted', 'Checking', 'Checking', 'Producing'),
                 bit(C)|bit(D)|bit(X), bit(C)|bit(D)|bit(X))

@dataclass(frozen=True)
class State:
    state: tuple
    live: int
    lease: int
    submitted: int = bit(PC)
    captured: int = 0
    intents: int = 0
    frontier: int = 0
    frozen: int = 0
    applied: int = 0
    evidence: tuple = (0,)*5
    reopens: tuple = (0,)*5
    incarnation: int = 0
    online: bool = True
    crashed: bool = False
    mode: str = 'Running'
    published: bool = False
    closed: int = bit(P)|bit(Q)
    obligations: int = 0

class Model:
    def __init__(self, d_reports_dependency=True, mutant=None, supersede=True, invalid_kind=None):
        self.invalid_kind=invalid_kind
        self.invalid=bit(PD) if invalid_kind else 0
        self.targets = (bit(P), bit(P)|bit(Q) if d_reports_dependency else 0)
        self.deps = bit(PC) | (bit(PD) if d_reports_dependency else 0)
        self.mutant = mutant
        self.supersede = supersede

    def providers(self, s):
        return sum(bit(n) for n in range(5)
                   if any(self.targets[k] & bit(n) for k in packet_members(s.intents)))

    def pending_for(self, s, n):
        return sum(bit(k) for k in packet_members(s.submitted) if SOURCES[k] == n)

    def evidence_for(self, s, n):
        return sum(bit(k) for k in packet_members(s.intents) if self.targets[k] & bit(n))

    def goal(self, s):
        return s.mode == 'Superseded' or (s.applied and all(x in {'Accepted','ProducerReady'} for x in s.state))

    def invariants(self, s):
        tests = {
            'TypeOK': not (s.captured & ~s.submitted) and not (s.intents & ~(s.captured & self.deps))
                      and not (s.applied & ~s.intents),
            'FrontierCompleteness': not s.intents or bool(s.frozen & ~s.closed) or s.frontier == s.submitted,
            'QuiescenceBeforeStale': all(not ((s.live|s.lease)&bit(n))
                for n in range(5) if s.state[n] in {'Stale','RepairReady','ProducerReady'}),
            'NoAdmissionBeforeApply': not (s.incarnation & s.frozen) or bool(s.applied),
            'AllPeerEvidenceBeforeApply': not s.applied or
                (not (s.submitted & ~s.captured) and not ((s.submitted & self.deps) & ~s.intents)),
            'InvalidRouteEvidenceRetained': not ((s.captured & self.invalid) & ~s.obligations),
            'IneligibleReportsNeverProviderTargets': not (s.intents & self.invalid),
            'OneReopenPerWave': all(x <= 1 for x in s.reopens),
            'JoinedProviderEvidence': not s.applied or all(s.evidence[n] == self.evidence_for(s,n) for n in range(5)),
            'TargetsNotStaledAsConsumers': not s.applied or all(s.state[n] != 'Stale' for n in members(self.providers(s))),
            'NoReplacementWithOldFence': all((s.incarnation & s.live & s.lease & bit(n))
                for n in range(5) if s.state[n] in {'Repairing','RepairChecking'}),
            'RepairCheckersAwaitAcceptedInputs': s.state[Q] != 'RepairChecking' or s.state[P] == 'Accepted',
            'LiveProcessKeepsLease': not (s.live & ~s.lease),
            'SupersessionDominates': s.mode != 'Superseded' or (not s.applied and not any(s.reopens)),
            'NoInvalidatedPublication': not s.published,
        }
        return [name for name, ok in tests.items() if not ok]

    def next(self, s):
        if not s.online:
            yield 'Restart', replace(s, online=True)
            return
        if not s.crashed:
            yield 'Crash', replace(s, online=False, crashed=True)
        if self.supersede and s.mode == 'Running' and not s.applied:
            yield 'Supersede', replace(s, mode='Superseded')
        if s.mode == 'Running' and not s.submitted & bit(PD) and not s.closed & bit(D) and not s.incarnation & bit(D) and s.lease & bit(D) and s.state[D] in {'Checking','CancelRequested'}:
            yield 'Submit(PD)', replace(s, submitted=s.submitted|bit(PD))
        for n in range(5):
            pending = self.pending_for(s,n)
            if not s.incarnation & bit(n) and s.live & bit(n) and (pending or s.state[n] == 'CancelRequested'):
                yield f'Reap({NAMES[n]})', replace(s, live=s.live & ~bit(n))
            if not s.incarnation & bit(n) and not s.live & bit(n) and not s.closed & bit(n):
                frontier=s.frontier | pending if s.frozen & bit(n) else s.frontier
                if self.mutant == 'missing_frontier':
                    frontier=s.frontier
                yield f'SealAssignment({NAMES[n]})', replace(s,closed=s.closed|bit(n),frontier=frontier)
            if not s.incarnation & bit(n) and s.closed & bit(n) and s.lease & bit(n) and not s.live & bit(n) and not (pending & ~s.captured):
                yield f'Release({NAMES[n]})', replace(s, lease=s.lease & ~bit(n))
        for k in packet_members(s.submitted & ~s.captured):
            if not s.live & bit(SOURCES[k]):
                if self.mutant == 'circular_capture' and s.intents and s.state[SOURCES[k]] != 'Stale':
                    continue
                obligations = s.obligations | (bit(k) & self.invalid)
                if self.mutant == 'drop_invalid_obligation':
                    obligations=s.obligations
                yield f'Capture(P{k})', replace(s, captured=s.captured|bit(k),obligations=obligations)
        for k in packet_members((s.captured & self.deps) & ~s.intents):
            if s.mode != 'Running' or s.applied:
                continue
            frozen = ALL
            state = tuple('CancelRequested' if not self.pending_for(s,n) and (s.live|s.lease)&bit(n) else s.state[n] for n in range(5))
            frontier = s.frontier | s.submitted
            if self.mutant == 'missing_frontier':
                frontier = s.frontier | bit(k)
            if self.mutant == 'premature_stale':
                state = tuple('Stale' if frozen & bit(n) and not self.targets[k] & bit(n) else state[n] for n in range(5))
            yield f'Register(P{k})', replace(s, intents=s.intents|bit(k), frozen=frozen,
                                            frontier=frontier, state=state)
        drained = not (s.frontier & ~s.captured) and not ((s.frontier & self.deps) & ~s.intents)
        quiet = not ((s.live|s.lease) & s.frozen) and not (s.frozen & ~s.closed)
        if self.mutant == 'lease_ignored':
            quiet = not (s.live & s.frozen) and not (s.frozen & ~s.closed)
        if s.mode == 'Running' and s.intents and not s.applied and drained and quiet:
            providers = self.providers(s)
            state = tuple('RepairReady' if providers & bit(n) else 'Stale' if s.frozen & bit(n) else s.state[n] for n in range(5))
            if self.mutant == 'per_packet_stale':
                # Wrong: staling consumers of PC after promoting targets from PD.
                state = setat(state, Q, 'Stale') if s.intents == 3 else state
            yield 'Apply', replace(s, applied=s.intents, state=state,
                evidence=tuple(self.evidence_for(s,n) for n in range(5)),
                reopens=tuple(s.reopens[n] + bool(providers & bit(n)) for n in range(5)))
        if s.mode == 'Running' and s.applied:
            providers = self.providers(s)
            for n in members(providers):
                if s.state[n] == 'RepairReady' and not (s.live|s.lease)&bit(n):
                    yield f'StartRepair({NAMES[n]})', replace(s, state=setat(s.state,n,'Repairing'), live=s.live|bit(n),lease=s.lease|bit(n),incarnation=s.incarnation|bit(n))
                if s.state[n] == 'Repairing':
                    yield f'FinishRepair({NAMES[n]})', replace(s,state=setat(s.state,n,'RepairCheckerReady'),live=s.live&~bit(n),lease=s.lease&~bit(n))
                if s.state[n] == 'RepairCheckerReady' and (n != Q or s.state[P] == 'Accepted' or self.mutant == 'checker_ignores_provider'):
                    yield f'StartRepairChecker({NAMES[n]})', replace(s,state=setat(s.state,n,'RepairChecking'),live=s.live|bit(n),lease=s.lease|bit(n))
                if s.state[n] == 'RepairChecking':
                    yield f'FinishRepairChecker({NAMES[n]})', replace(s,state=setat(s.state,n,'Accepted'),live=s.live&~bit(n),lease=s.lease&~bit(n))
            if all(s.state[n] == 'Accepted' for n in members(providers)) and any(s.state[n] == 'Stale' for n in members(s.frozen & ~providers)):
                yield 'ReleaseConsumers', replace(s, state=tuple('ProducerReady' if s.frozen&~providers&bit(n) else s.state[n] for n in range(5)))
            if self.mutant == 'replay_reopen':
                n = P
                yield 'ReplayOriginalPacketUnsafely', replace(s,reopens=setat(s.reopens,n,s.reopens[n]+1))
            if self.mutant == 'old_ack_clears_replacement' and s.state[P] == 'Repairing':
                yield 'AcknowledgeOldFenceUnsafely', replace(s,live=s.live&~bit(P),lease=s.lease&~bit(P))
        if self.mutant == 'invalidated_pass_publishes' and s.frozen and s.captured & bit(PD) and not self.targets[PD]:
            yield 'PublishInvalidatedPassUnsafely', replace(s,published=True)
        if self.mutant == 'route_ineligible_report' and s.captured & self.invalid and not s.intents & self.invalid:
            yield 'RegisterIneligibleReportUnsafely', replace(s,intents=s.intents|self.invalid)
        if self.mutant == 'late_setup_launch' and s.frozen and not s.applied and s.state[X] == 'CancelRequested':
            yield 'LateSetupLaunchUnsafely', replace(s,state=setat(s.state,X,'Producing'),live=s.live|bit(X),lease=s.lease|bit(X),incarnation=s.incarnation|bit(X))
        # Replay, stale old-fence acknowledgement, failed cleanup are stutters.
        yield 'ReplayOrFailedCleanup', s


def trace_to(s, pred):
    trace=[]
    while pred[s] is not None:
        prev,label=pred[s]
        trace.append(label)
        s=prev
    return list(reversed(trace))

def explore(model):
    init=initial(); pred={init:None}; todo=deque([init]); reverse=defaultdict(set)
    transitions=0; goal_states=[]; terminal_signatures=set(); sample={}
    while todo:
        s=todo.popleft()
        errors=model.invariants(s)
        if errors:
            return {'kind':'counterexample','invariants':errors,'states_visited':len(pred),'trace':trace_to(s,pred),'state':asdict(s)}
        if s.intents and s.submitted == 3 and s.state[D] == 'CancelRequested':
            sample.setdefault('late_raw_result_after_freeze',trace_to(s,pred))
        if s.state[P] == 'Repairing' and s.state[Q] == 'Repairing':
            sample.setdefault('provider_coders_parallel',trace_to(s,pred))
        if model.goal(s):
            goal_states.append(s)
            if s.mode == 'Running':
                terminal_signatures.add((s.submitted,s.intents,s.state,s.evidence,s.reopens))
                key='both_submitted' if s.submitted == 3 else 'peer_cancelled_before_submission'
                sample.setdefault(key,trace_to(s,pred))
        for label,nxt in model.next(s):
            transitions+=1
            if s.submitted & ~nxt.submitted or s.captured & ~nxt.captured:
                return {'kind':'counterexample','invariants':['ImmutableEvidenceMonotonic'],'trace':trace_to(s,pred)+[label]}
            reverse[nxt].add(s)
            if nxt not in pred:
                pred[nxt]=(s,label); todo.append(nxt)
    can_finish=set(goal_states); todo=deque(goal_states)
    while todo:
        s=todo.popleft()
        for prev in reverse[s]:
            if prev not in can_finish:
                can_finish.add(prev);todo.append(prev)
    dead=[s for s in pred if s not in can_finish]
    if dead:
        s=min(dead,key=lambda v:len(trace_to(v,pred)))
        return {'kind':'progress_counterexample','states':len(pred),'no_terminal_path_states':len(dead),'trace':trace_to(s,pred),'state':asdict(s)}
    # Source order should not change terminal joined state for one submitted set.
    grouped=defaultdict(set)
    for submitted,intents,state,evidence,reopens in terminal_signatures:
        grouped[submitted].add((intents,state,evidence,reopens))
    assert all(len(sigs)==1 for sigs in grouped.values()), 'Registration order changes joined result'
    return {'kind':'bounded_python_pass','states':len(pred),'transitions_including_stutters':transitions,
            'all_states_have_terminal_path':True,'terminal_signature_counts':{str(k):len(v) for k,v in grouped.items()},
            'positive_traces':sample}

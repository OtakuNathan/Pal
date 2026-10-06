#!/usr/bin/env python3
"""Small bounded correspondence refinements. Python exploration, not TLC."""
from dataclasses import dataclass,replace,asdict
from collections import deque,defaultdict

def explore(init,step,check,goal,witness=None):
    q=deque([init]);pred={init:None};rev=defaultdict(set);goals=[];traces={};edges=0
    def trace(s):
        out=[]
        while pred[s] is not None:
            s0,label=pred[s];out.append(label);s=s0
        return out[::-1]
    while q:
        s=q.popleft();bad=check(s)
        if bad:return {'kind':'counterexample','invariants':bad,'trace':trace(s),'state':asdict(s)}
        if goal(s):goals.append(s)
        if witness:
            for name in witness(s):traces.setdefault(name,trace(s))
        for label,nxt in step(s):
            edges+=1;rev[nxt].add(s)
            if nxt not in pred:pred[nxt]=(s,label);q.append(nxt)
    finish=set(goals);q=deque(goals)
    while q:
        for p in rev[q.popleft()]:
            if p not in finish:finish.add(p);q.append(p)
    missing=set(pred)-finish
    if missing:
        s=min(missing,key=lambda s:len(trace(s)))
        return {'kind':'progress_counterexample','trace':trace(s),'state':asdict(s)}
    return {'kind':'bounded_python_pass','states':len(pred),'transitions':edges,'all_states_have_terminal_path':True,'witnesses':traces}

@dataclass(frozen=True)
class Setup:
    stage:str='Admitted'
    frozen:bool=False
    starting:bool=False
    old_process:bool=False
    lease:bool=False
    closed:bool=False
    stale:bool=False
    replacement:bool=False
    new_process:bool=False

def setup_model(mutant=None):
    def step(s):
        if not s.frozen:
            yield 'Freeze',replace(s,frozen=True)
        if s.stage=='Admitted' and (not s.frozen or mutant=='create_after_freeze'):
            yield 'AcquireLease',replace(s,stage='Leased',lease=True)
        if s.stage=='Leased' and (not s.frozen or mutant=='create_after_freeze'):
            yield 'CreateAssignment',replace(s,stage='Assigned')
        if s.stage=='Assigned' and not s.frozen:
            yield 'ReserveSpawn',replace(s,stage='Starting',starting=True)
        if s.starting:
            yield 'FinishOwnedSpawn',replace(s,stage='Running',starting=False,old_process=True)
            if s.frozen:yield 'AbortOwnedSpawn',replace(s,stage='Cancelled',starting=False)
        if s.frozen and s.old_process:
            yield 'ReapExactOldOwner',replace(s,old_process=False)
        if s.frozen and not s.closed and not s.old_process and (not s.starting or mutant=='ack_while_starting'):
            yield 'CloseSetupAndAssignment',replace(s,closed=True)
        if s.closed and s.lease:
            yield 'ReleaseExactOldLease',replace(s,lease=False)
        if s.closed and s.frozen and not s.starting and not s.old_process and not s.lease and not s.stale:
            yield 'MarkStale',replace(s,stale=True)
        if s.stale and not s.replacement:
            yield 'StartReplacement',replace(s,replacement=True,new_process=True)
        if mutant=='reusable_invocation_cleanup' and s.replacement and s.new_process:
            yield 'CleanupByReusedInvocationId',replace(s,new_process=False)
        yield 'ReplayOldCleanup',s
    def check(s):
        checks={'NoAckWithStartingOwner':not s.closed or not(s.starting or s.old_process),
                'StaleMeansOldOwnerGone':not s.stale or (s.closed and not(s.starting or s.old_process or s.lease)),
                'OldCleanupCannotTouchReplacement':not s.replacement or s.new_process,
                'ClosedSetupCannotAcquireNewLease':not s.closed or not(s.stage=='Leased' and s.lease)}
        # A previously leased setup may legitimately close then release its lease;
        # new creation is checked as a transition obligation in the unsafe action.
        checks.pop('ClosedSetupCannotAcquireNewLease')
        return [k for k,v in checks.items() if not v]
    return explore(Setup(),step,check,lambda s:s.stale,
        lambda s:[name for name,ok in [('no_assignment_freeze',s.frozen and s.stage=='Admitted'),
                                      ('spawn_finishes_after_freeze',s.frozen and s.stage=='Running' and s.old_process),
                                      ('replacement_survives_old_cleanup',s.replacement and s.new_process)] if ok])

@dataclass(frozen=True)
class Join:
    registered:frozenset=frozenset()
    applied:frozenset=frozenset()
    frozen:frozenset=frozenset()
    revision:int=0
    cached:frozenset=frozenset()
    cached_revision:int=0
    reopened:tuple=(0,0,0)
    packet_applies:tuple=(0,0,0)
    bad_join:bool=False

def join_model(bridge=False,mutant=None):
    scopes=({'A':frozenset('PCX'),'B':frozenset('QDX')} if not bridge else
            {'A':frozenset('PCX'),'B':frozenset('QDY'),'J':frozenset('PREXY')})
    targets={'A':{0},'B':{1},'J':{0,2}}
    packet_index={'A':0,'B':1,'J':2}
    all_packets=frozenset(scopes)
    def scope(ks):return frozenset().union(*(scopes[k] for k in ks))
    def component(k,universe):
        result={k}
        while True:
            nxt=result|{j for j in universe if scopes[j]&scope(result)}
            if nxt==result:return frozenset(result)
            result=nxt
    def step(s):
        for k in all_packets-s.registered:
            yield f'Register({k})',replace(s,registered=s.registered|{k},frozen=s.frozen|scopes[k],revision=s.revision+1)
        for k in s.registered-s.applied:
            yield f'PrepareCachedSeal({k})',replace(s,cached=component(k,s.registered-s.applied),cached_revision=s.revision)
        if s.cached and not(s.cached&s.applied):
            valid=s.cached_revision==s.revision and all(component(k,s.registered-s.applied)==s.cached for k in s.cached)
            if valid or mutant=='apply_cached_scope':
                counts=tuple(s.reopened[n]+any(n in targets[k] for k in s.cached) for n in range(3))
                yield 'ApplyWholeRevalidatedComponent',replace(s,applied=s.applied|s.cached,reopened=counts,
                    packet_applies=tuple(s.packet_applies[n]+any(packet_index[k]==n for k in s.cached) for n in range(3)),
                    bad_join=s.bad_join or not valid)
        yield 'CrashRestartPreservesLedger',s
    def check(s):
        checks={'CurrentPendingComponentJoinedAtomically':not s.bad_join,
                'OnePacketProviderApplication':all(x<=1 for x in s.packet_applies),
                'AppliedScopeStillFenced':scope(s.applied)<=s.frozen}
        return [k for k,v in checks.items() if not v]
    return explore(Join(),step,check,lambda s:s.applied==all_packets,
        lambda s:[name for name,ok in [('separate_later_cohorts',len(s.applied)>0 and s.applied!=all_packets),
                                     ('stale_cached_seal_detectable',bool(s.cached) and s.cached_revision!=s.revision),
                                     ('bridge_registered_last',bridge and s.registered==all_packets and s.cached==frozenset({'A'}))] if ok])

@dataclass(frozen=True)
class Obligation:
    captured:frozenset=frozenset()
    required:frozenset=frozenset()
    resolved:frozenset=frozenset()
    targets:frozenset=frozenset()
    next_candidate:bool=False
    passed:bool=False
    reinvalidated:bool=False

def obligation_model(mutant=None):
    # Shared scope validation, not dominant-class selection, authorizes targets.
    cases={'dependency':frozenset({'dep_case'}),'module':frozenset({'module_case'}),
           'invalid_scope':frozenset({'correction_case'}),'contract_only':frozenset({'contract_case'}),
           'mixed':frozenset({'mixed_dep_case','mixed_module_case'})}
    targets={'dependency':frozenset({'P'}),'module':frozenset(),'invalid_scope':frozenset(),
             'contract_only':frozenset(),'mixed':frozenset({'Q'})}
    receipts=frozenset(cases)
    def required(captured):return frozenset().union(*(cases[r] for r in captured))
    def wanted_targets(captured):return frozenset().union(*(targets[r] for r in captured))
    def step(s):
        if not s.next_candidate:
            for r in receipts-s.captured:
                retain=cases[r]
                if mutant=='drop_module_findings' and r in {'module','mixed'}:retain=frozenset(c for c in retain if 'module' not in c)
                routes=targets[r]
                if mutant=='route_invalid_scope' and r in {'invalid_scope','contract_only'}:routes=frozenset({'X'})
                yield f'CaptureValidatedReceipt({r})',replace(s,captured=s.captured|{r},required=s.required|retain,targets=s.targets|routes)
        if s.captured==receipts and not s.next_candidate:
            yield 'ReplaceCandidateCarryExactRefs',replace(s,next_candidate=True,required=frozenset() if mutant=='drop_history_on_replacement' else s.required)
        if s.next_candidate and not s.reinvalidated:
            yield 'InvalidateAgainCarryExactRefs',replace(s,reinvalidated=True,resolved=frozenset(),required=frozenset() if mutant=='drop_history_on_second_invalidation' else s.required)
        if s.next_candidate:
            for case in s.required-s.resolved:
                yield f'ExplicitlyResolve({case})',replace(s,resolved=s.resolved|{case})
            if s.reinvalidated and not s.passed and (s.required<=s.resolved or mutant=='pass_skips_old_case'):
                yield 'NextCheckerPASS',replace(s,passed=True)
        yield 'ReplayCapturedReference',s
    def check(s):
        checks={'CapturedFindingsRemainRequired':required(s.captured)<=s.required,
                'PassRequiresExplicitResolution':not s.passed or required(s.captured)<=s.resolved,
                'OnlyValidatedScopeTargetsProviders':s.targets==wanted_targets(s.captured)}
        return [k for k,v in checks.items() if not v]
    return explore(Obligation(),step,check,lambda s:s.passed,
        lambda s:['mixed_and_invalid_findings_carried'] if s.next_candidate and s.required==required(receipts) else [])

#!/usr/bin/env python3
"""Late upstream cohort bounded Python refinement, not TLC."""
from dataclasses import dataclass,replace
from .refinements import explore

@dataclass(frozen=True)
class Late:
    p:str='RepairReady'
    live:bool=False
    lease:bool=False
    fence:bool=False
    closed:bool=False
    registered:bool=False
    applied:bool=False
    q:str='Accepted'
    q_version:int=0
    binding:int=0
    required:frozenset=frozenset({'old-P-case'})
    resolved:frozenset=frozenset()
    resolution_candidate:int=-1
    candidate:int=0
    old_check:bool=False
    a_receipt:int=1
    b_receipt:int=0

def run(mutant=None):
    def step(s):
        if s.p=='RepairReady' and not s.fence:
            yield 'StartPProducer',replace(s,p='Producing',live=True,lease=True)
        if s.p=='Producing':
            yield 'FinishPProducer',replace(s,p='CheckerReady',live=False,lease=False)
        if s.p=='CheckerReady' and not s.fence and s.q=='Accepted':
            yield 'StartPChecker',replace(s,p='Checking',live=True,lease=True,binding=s.q_version)
        if s.p=='Checking' and not s.fence and s.resolved!=s.required:
            yield 'ResolvePCaseForCurrentCandidate',replace(s,resolved=s.required,resolution_candidate=s.candidate)
        if s.p=='Checking' and not s.fence and s.q=='Accepted' and s.binding==s.q_version and s.required<=s.resolved:
            yield 'PCheckerPASS',replace(s,p='Accepted',live=False,lease=False)
        if not s.registered:
            yield 'RegisterLaterQPacket',replace(s,registered=True,fence=True,old_check=s.p=='Checking',p='CancelRequested')
        if s.fence and s.live:
            yield 'ReapExactOldP',replace(s,live=False)
        if s.fence and not s.live and s.lease:
            yield 'ReleaseExactOldPLease',replace(s,lease=False)
        if s.fence and not s.live and not s.lease and not s.closed:
            yield 'JoinOldPTaskAndAssignment',replace(s,closed=True)
        if s.registered and not s.applied and (s.closed and not s.live and not s.lease or mutant=='stale_active_prior_repair'):
            yield 'ApplyLaterQPacket',replace(s,applied=True,q='RepairReady',p='Stale',candidate=1,
                resolved=s.resolved if mutant=='reuse_old_case_resolution' else frozenset(),
                resolution_candidate=s.resolution_candidate if mutant=='reuse_old_case_resolution' else -1,
                required=frozenset() if mutant=='drop_prior_obligation' else s.required,b_receipt=1)
        if s.q=='RepairReady':
            yield 'RepairAndAcceptQ',replace(s,q='Accepted',q_version=1)
        if s.applied and s.q=='Accepted' and s.p=='Stale':
            yield 'ReleasePRepairBarrier',replace(s,fence=False,p='RepairReady')
        if mutant=='accept_old_binding' and s.old_check and s.fence:
            yield 'OldBoundPASSAfterFence',replace(s,p='Accepted')
        if mutant=='reapply_original_packet' and s.applied:
            yield 'ReapplyOriginalPPacket',replace(s,a_receipt=s.a_receipt+1)
        yield 'ReplayOldReceiptOrCleanup',s
    def check(s):
        checks={'NoPrematureStale':s.p!='Stale' or s.closed and not s.live and not s.lease,
                'NoOldBindingAcceptance':s.p!='Accepted' or not s.fence and s.q=='Accepted' and s.binding==s.q_version,
                'PriorObligationCarried':s.required==frozenset({'old-P-case'}),
                'PassUsesCurrentCandidateResolution':s.p!='Accepted' or s.required<=s.resolved and s.resolution_candidate==s.candidate,
                'OriginalPacketIsNotReapplied':s.a_receipt==1 and s.b_receipt<=1}
        return [k for k,v in checks.items() if not v]
    return explore(Late(),step,check,lambda s:s.applied and s.q_version==1 and s.p=='Accepted',
        lambda s:[name for name,ok in [('later_report_during_prior_producer',s.registered and s.live and not s.old_check),
                                      ('later_report_during_prior_checker',s.registered and s.old_check and s.live),
                                      ('prior_obligation_revalidated_new_binding',s.applied and s.p=='Accepted') ] if ok])

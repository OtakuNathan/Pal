---------------------- MODULE PromptCacheFixedAnchors ----------------------
\* Retired: historical economic/ACK model; current runtime uses PromptCacheTail.
EXTENDS Naturals, FiniteSets
CONSTANTS SkipFixed, UnprovenBase
VARIABLES turn, u, base, covered, cache, markers, phase, h, upper, requests
vars == <<turn,u,base,covered,cache,markers,phase,h,upper,requests>>
Init == /\ turn=0 /\ u=2 /\ base=0 /\ covered=0
        /\ cache \in SUBSET {1,2} /\ markers={} /\ phase="ready"
        /\ h=0 /\ upper \in {1,3} /\ requests=0
Submit == /\ phase="ready" /\ requests<4
          /\ markers'=IF SkipFixed THEN {1} ELSE {1,u}
          /\ phase'="sent" /\ requests'=requests+1
          /\ UNCHANGED <<turn,u,base,covered,cache,h,upper>>
Provider == /\ phase="sent"
            /\ h' \in ({0} \cup (cache \cap markers))
            /\ \A p \in (cache \cap markers): h'>=p
            /\ cache'=cache \cup markers /\ phase'="observe"
            /\ UNCHANGED <<turn,u,base,covered,markers,upper,requests>>
Observe == /\ phase="observe"
           /\ base'=IF UnprovenBase \/ h>upper THEN u ELSE IF h>0 /\ base=0 THEN 1 ELSE base
           /\ covered'=IF h>covered THEN h ELSE covered
           /\ phase'="ready"
           /\ UNCHANGED <<turn,u,cache,markers,h,upper,requests>>
BeginTurn == /\ phase="ready" /\ turn=0
             /\ turn'=1 /\ u'=4 /\ base'=IF covered>=1 THEN 1 ELSE 0
             /\ covered'=IF covered>=1 THEN 1 ELSE 0
             /\ upper'=3
             /\ UNCHANGED <<cache,markers,phase,h,requests>>
Evict == /\ phase="ready"
         /\ cache' \in SUBSET cache
         /\ UNCHANGED <<turn,u,base,covered,markers,phase,h,upper,requests>>
Next == Submit \/ Provider \/ Observe \/ BeginTurn \/ Evict
Spec == Init /\ [][Next]_vars
FixedMarkers == phase \in {"sent","observe"} => {1,u} \subseteq markers
CoveredBase == base<=covered
=============================================================================

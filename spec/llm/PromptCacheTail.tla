-------------------------- MODULE PromptCacheTail --------------------------
EXTENDS Naturals, Sequences, FiniteSets, TLC

\* Provider cache contents and receipt values are not controller inputs.
\* S=0, T=1, tail block positions=2..4. Epoch changes model turn, content,
\* endpoint or mode changes, including scope eviction/recreation. Epoch abstracts
\* both the nonreused scope incarnation and its content generation.
\* Build freezes a request; Submit alone records C.
CONSTANT CommitOnPrepare
VARIABLES epoch, mode, history, sequence, planned, receipts, cache, phase, admitted
vars == <<epoch, mode, history, sequence, planned, receipts, cache, phase, admitted>>
Modes == {"implicit", "hybrid", "explicit"}
Positions == 2..4
Last(h) == IF Len(h)=0 THEN 1 ELSE h[Len(h)]
KeepTwo(h) == IF Len(h)>2 THEN SubSeq(h, Len(h)-1, Len(h)) ELSE h
Previous(h,c) == {h[i] : i \in 1..Len(h)} \cap 2..(c-1)
MaxOrEmpty(s) == IF s={} THEN {} ELSE {CHOOSE x \in s: \A y \in s: x>=y}
Markers(m,h,c) == IF m="implicit" THEN {} ELSE
                  IF m="hybrid" THEN {0,1} ELSE {0,1,c} \cup MaxOrEmpty(Previous(h,c))

Init == /\ epoch=0 /\ mode \in Modes /\ history= << >> /\ sequence=0
        /\ planned=[epoch |-> 0, mode |-> mode, seq |-> 0, c |-> 2, markers |-> {}]
        /\ receipts=0 /\ cache={} /\ phase="idle" /\ admitted={}

Build == /\ \E c \in Positions:
              /\ c>=Last(history)
              /\ planned'=[epoch |-> epoch, mode |-> mode, seq |-> sequence+1,
                           c |-> c, markers |-> Markers(mode,history,c)]
              /\ history'=IF CommitOnPrepare /\ mode="explicit" /\ c>Last(history)
                            THEN KeepTwo(Append(history,c)) ELSE history
         /\ phase'="prepared"
         /\ UNCHANGED <<epoch,mode,sequence,receipts,cache,admitted>>

\* Submit is the transport admission callback (or first response frame), not Build.
Submit == /\ phase="prepared" /\ planned.epoch=epoch /\ planned.mode=mode
          /\ planned.seq>sequence /\ sequence<4
          /\ sequence'=planned.seq
          /\ history'=IF mode="explicit" /\ planned.c>Last(history)
                       THEN KeepTwo(Append(history,planned.c)) ELSE history
          /\ phase'="admitted" /\ admitted'=admitted \cup {planned.c}
          /\ UNCHANGED <<epoch,mode,planned,receipts,cache>>

Reject == /\ phase="prepared" /\ phase'="rejected"
          /\ UNCHANGED <<epoch,mode,history,sequence,planned,receipts,cache,admitted>>

Observe == /\ receipts<2
           /\ receipts'=receipts+1
           \* Arbitrary loss, successful write, zero write or routing change.
           /\ cache' \in SUBSET (0..4)
           /\ UNCHANGED <<epoch,mode,history,sequence,planned,phase,admitted>>

Invalidate == /\ epoch<2 /\ epoch'=epoch+1 /\ mode' \in Modes
              /\ history'= << >> /\ admitted'={}
              /\ UNCHANGED <<sequence,planned,receipts,cache,phase>>

Next == Build \/ Submit \/ Reject \/ Observe \/ Invalidate
Spec == Init /\ [][Next]_vars
AdmittedHistory == {history[i] : i \in 1..Len(history)} \subseteq admitted
BoundedHistory == Len(history)<=2
OrderedHistory == \A i,j \in 1..Len(history): i<j => history[i]<history[j]
MarkerCapacity == Cardinality(planned.markers)<=4
FixedAnchors == planned.markers={} \/ {0,1} \subseteq planned.markers
NonExplicitHasNoTail == mode#"explicit" => history= << >>
SubmissionOnly == [][(history'#history /\ epoch'=epoch) =>
                    (sequence'>sequence /\ planned.epoch=epoch /\ mode="explicit")]_vars
=============================================================================

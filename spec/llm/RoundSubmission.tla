-------------------------- MODULE RoundSubmission --------------------------
EXTENDS Naturals, Sequences, FiniteSets, TLC
CONSTANTS MaxFacts, Mutant
VARIABLE s
Facts(seq) == {seq[i] : i \in 1..Len(seq)}
Init == s = [left |-> <<>>, right |-> <<>>, accepted |-> <<>>,
  native |-> {}, summarized |-> {}, next |-> 1, epoch |-> 0,
  pending |-> "none", capture |-> <<>>, captureEpoch |-> 0,
  compact |-> FALSE, compactLeft |-> <<>>, compactRight |-> <<>>, bad |-> FALSE]
AppendFact == /\ s.next <= MaxFacts
          /\ s' = [s EXCEPT !.right = Append(@, s.next),
                    !.accepted = Append(@, s.next), !.native = @ \cup {s.next},
                    !.next = @ + 1]
Prepare == /\ s.pending = "none" /\ ~s.compact /\ s.right # <<>>
           /\ s' = [s EXCEPT !.pending = "prepared", !.capture = s.right,
                      !.captureEpoch = s.epoch]
Send == /\ s.pending = "prepared"
        /\ s' = [s EXCEPT !.pending = "submitted"]
Commit == /\ (s.pending = "submitted" \/ (Mutant = "early" /\ s.pending = "prepared"))
          /\ s.captureEpoch = s.epoch /\ ~s.compact
          /\ SubSeq(s.right, 1, Len(s.capture)) = s.capture
          /\ s' = [s EXCEPT !.left = @ \o s.capture,
                   !.right = SubSeq(@, Len(s.capture) + 1, Len(@)),
                   !.pending = "none", !.bad = @ \/ (s.pending # "submitted")]
ResolveWithoutCommit == /\ s.pending # "none"
                        /\ s' = [s EXCEPT !.pending = "none"]
Reset == /\ s.epoch < 1
         /\ s' = [s EXCEPT !.left = <<>>, !.right = <<>>, !.accepted = <<>>,
                    !.native = {}, !.summarized = {}, !.epoch = @ + 1,
                    !.compact = FALSE]
BeginCompact == /\ ~s.compact /\ s.pending = "none" /\ s.left # <<>>
                /\ Facts(s.left) \ s.summarized # {}
                /\ s' = [s EXCEPT !.compact = TRUE, !.compactLeft = s.left, !.compactRight = <<>>]
\* A fact is an accepted, protocol-closed unit. Native compaction may
\* capture a continuous completed R prefix without claiming a normal send.
\* Capturing never moves the live cut; only a successful install does.
BeginNativeCompact == /\ ~s.compact /\ s.pending = "none"
                      /\ s.right # <<>>
                      /\ \E n \in 1..Len(s.right):
                           s' = [s EXCEPT !.compact = TRUE,
                             !.compactLeft = s.left,
                             !.compactRight = SubSeq(s.right, 1, n),
                             !.left = IF Mutant = "early_native_cut"
                                      THEN @ \o SubSeq(s.right, 1, n) ELSE @]
InstallCompact == /\ s.compact
                  /\ s' = [s EXCEPT !.compact = FALSE,
                            !.summarized = @ \cup Facts(s.compactLeft \o s.compactRight),
                            !.native = @ \ Facts(s.compactLeft \o s.compactRight),
                            !.left = @ \o s.compactRight,
                            !.right = IF Mutant = "erase_right" THEN <<>>
                                      ELSE SubSeq(@, Len(s.compactRight) + 1, Len(@))]
CancelCompact == /\ s.compact /\ s' = [s EXCEPT !.compact = FALSE]
DropNative == /\ Mutant = "strip_reasoning" /\ s.native # {}
              /\ s' = [s EXCEPT !.native = {}]
Next == AppendFact \/ Prepare \/ Send \/ Commit \/ ResolveWithoutCommit \/ Reset
        \/ BeginCompact \/ BeginNativeCompact \/ InstallCompact \/ CancelCompact \/ DropNative \/ UNCHANGED s
Spec == Init /\ [][Next]_s /\ WF_s(ResolveWithoutCommit)
Conservation == s.left \o s.right = s.accepted
NativeRetained == Facts(s.accepted) \ s.summarized \subseteq s.native
SubmissionRequired == ~s.bad
FrozenDuringCompact == s.compact => s.left = s.compactLeft
CapturedTailStable == s.compact => SubSeq(s.right, 1, Len(s.compactRight)) = s.compactRight
PendingTerminates == (s.pending # "none") ~> (s.pending = "none")
=============================================================================

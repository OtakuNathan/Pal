----------------------- MODULE ManagedTaskPromotion -----------------------
VARIABLES foreground, candidate, intent, head, receipt, conflict, dirty
vars == <<foreground,candidate,intent,head,receipt,conflict,dirty>>
Init ==
    /\ foreground = "none" /\ candidate = FALSE /\ intent = FALSE
    /\ head = "base" /\ receipt = FALSE /\ conflict = FALSE /\ dirty = FALSE
CaptureForeground ==
    /\ foreground = "none" /\ ~intent /\ ~receipt
    /\ foreground' \in {"disjoint", "overlap"} /\ head' = "foreground"
    /\ UNCHANGED <<candidate,intent,receipt,conflict,dirty>>
AcceptCandidate ==
    /\ ~candidate /\ candidate' = TRUE
    /\ UNCHANGED <<foreground,intent,head,receipt,conflict,dirty>>
Prepare ==
    /\ candidate /\ ~intent /\ ~receipt /\ ~conflict /\ ~dirty
    /\ IF foreground = "overlap"
        THEN /\ conflict' = TRUE /\ intent' = FALSE
        ELSE /\ intent' = TRUE /\ conflict' = FALSE
    /\ UNCHANGED <<foreground,candidate,head,receipt,dirty>>
Advance ==
    /\ intent /\ ~dirty /\ head # "target"
    /\ head' = "target"
    /\ UNCHANGED <<foreground,candidate,intent,receipt,conflict,dirty>>
Settle ==
    /\ intent /\ head = "target" /\ ~dirty
    /\ receipt' = TRUE /\ intent' = FALSE
    /\ UNCHANGED <<foreground,candidate,head,conflict,dirty>>
InterruptedCheckout ==
    /\ intent /\ ~receipt /\ ~dirty /\ dirty' = TRUE
    /\ UNCHANGED <<foreground,candidate,intent,head,receipt,conflict>>
Resolve ==
    /\ dirty /\ intent /\ dirty' = FALSE
    /\ UNCHANGED <<foreground,candidate,intent,head,receipt,conflict>>
Next == CaptureForeground \/ AcceptCandidate \/ Prepare \/ Advance \/ Settle
        \/ InterruptedCheckout \/ Resolve
Spec == Init /\ [][Next]_vars
ReceiptBindsAccepted == receipt => candidate /\ head = "target" /\ ~intent
NoOverwriteOnConflict == conflict => foreground = "overlap" /\ head = "foreground" /\ candidate /\ ~receipt
NoUnjournaledPromotion == head = "target" => candidate /\ (intent \/ receipt)
=============================================================================

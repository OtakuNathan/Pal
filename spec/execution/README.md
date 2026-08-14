# Execution state-machine models

`FileResultAuthorization.tla` models result-owned authorization shared by Pal
and Bunshin file tools.  File content is abstracted to bounded versions and
line regions; Python refines those regions with the canonical line map and
refines the atomic mutation action with digest-checked filesystem CAS.

Run the model with the pinned TLC jar:

```bash
scripts/check_execution_tla.sh /path/to/tla2tools.jar
```

Tool-result compaction, result expiry, and logical-session retirement all use
the same `RetireResult` transition.  Pager payload retention is not mutation
authority; replaying an exact page creates a new result-owned contribution.

# tasking

Owns:
- task and work-order semantics
- checkpoint and ledger contracts
- worker orchestration boundary
- workspace and continuity governance

Does not own:
- global governance
- tool runtime
- channel transport
- memory ranking

Exposes:
- `TaskContextPack`
- worker lifecycle events
- `TaskingService`
- `WorkerEventSource`

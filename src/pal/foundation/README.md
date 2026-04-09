# foundation

Owns:
- async and process-facing infrastructure contracts
- database lifecycle, transaction scope, external-schema assumptions, raw SQL hook registry
- shared low-level primitives that do not carry business semantics

Does not own:
- memory semantics
- channel routing policy
- execution governance
- tasking rules

Exposes:
- `EventEnvelope`
- `PalV2Database`
- `RawSQLHookRegistry`
- `RepositoryBase`

Rule:
- schema migration is external to `PalV2`
- runtime initialization assumes the database has already been migrated or
  otherwise prepared before startup

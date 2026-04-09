# bootstrap

Owns:
- in-process runtime composition

Does not own:
- ongoing lifecycle management
- user-facing runtime behavior
- business object truth beyond setup wiring
- first-run provisioning
- database-file creation or association

Exposes:
- `RuntimeComposer`
- `compose_runtime`

Boundary:
- `supervisor` provisions Pal instances, database files, and first-run defaults
- `bootstrap` only composes the in-process runtime from the already-provisioned database

# Plan for Moving Memory Backend Maintenance into the Pal CLI

Date: 2026-10-15
Status: Implemented into source code per the confirmed scope; see the [implementation record](pal_tool_consolidation_implementation.md) for targeted verification. Not yet installed or activated.

## Confirmed Direction

Memory backend maintenance is no longer provided as model tools; it is uniformly performed through the official Pal CLI. Changing the embedding provider involves upgrade scripts, vector space compatibility, and index rebuilds, and should be handled as a managed migration rather than a hot backend switch during ordinary conversation.

The model continues to use everyday memory capabilities: recall, remember, update, forget, plus memory history and health diagnostics as needed. Removing backend maintenance from the tool surface does not change the meaning of `mem_ref` or references to the original provider.

## Tool Surface Adjustments

| Current tool | Plan |
| --- | --- |
| `attach_memory_provider` | Withdraw from the model tool surface; move into CLI lifecycle commands |
| `detach_memory_provider` | Withdraw from the model tool surface; move into CLI lifecycle commands |
| `set_active_memory_provider` | Withdraw from the model tool surface; the CLI manages storage backend selection and how it takes effect |
| `refresh_memory_provider_indexes` | Withdraw from the model tool surface; the CLI invokes index maintenance or migration jobs |
| `list_memory_providers` | The backend inventory moves into the CLI; active provider information needed for everyday diagnostics is merged into `inspect_memory_state` |
| `inspect_memory_provider_inventory` | Detailed backend inventory, index, and embedding diagnostics move into the CLI; the model keeps only a necessary health summary |

The six public entry points above are planned for withdrawal; no searchable maintenance aliases or equivalent general-purpose model management tools will be kept. Internal provider methods remain usable by the runtime and the CLI owner; withdrawing tools does not mean deleting the implementation.

`inspect_memory_state` is retained, providing the memory module's and the current backend's availability, necessary counts, brief failure reasons, and explicit maintenance recommendations. Specific database paths, vector configuration, migration details, and index internal state are left to the CLI.

`manage_memory_dreaming` mixes automatic task configuration with memory content curation; this round does not treat it wholesale as a backend switch. Whether its configuration part moves into the CLI, and how the content curation entry point is consolidated, will be reviewed separately later. Memory candidate approval and submission rules are also unchanged by this backend maintenance migration.

## Two Kinds of Provider Changes Must Be Distinguished

- **L3 storage backend change**: changes the storage instance used by everyday memory operations, involving resource lifecycle, data ownership, and how the change takes effect.
- **Embedding provider / model change**: may change the vector space, involving handling of old vectors, recomputation, and index rebuilds. Switching the active L3 provider cannot substitute for this process.

Migration scripts should identify the provider, model version, dimensions, and any configuration affecting vector compatibility. Identical vector dimensions do not mean compatible vector spaces. Merely changing the transport address versus replacing the vector space also cannot be handled as the same kind of migration across the board; the script should check compatibility before deciding whether a rebuild is required.

## Responsibilities of the CLI and Upgrade Scripts

Command names will be aligned with the existing Pal CLI at implementation time. The following is the capability scope and does not mean these commands already exist:

1. Query the backend inventory, current configuration, connection status, and index health.
2. Manage backend attach / detach / select, and state explicitly whether it takes effect online or on next startup.
3. Query the target embedding configuration and its compatibility with current data, and generate a migration plan.
4. Execute data or vector index migration via the official upgrade script, reporting progress, failures, and recovery steps.
5. Support index inspection, refresh, and necessary recomputation, without relying on the model hand-editing the database or assembling ad hoc SQL.

Online operations must be performed through the owner of the running instance; a separate CLI process must not directly modify live Python objects, nor may it merely edit a configuration file and then claim the running instance has completed the switch.

The upgrade process must be re-runnable or able to resume interrupted jobs; incompatible old and new vectors must not be mixed. Before the switch completes, recoverable data and configuration state is preserved; afterwards, verify the configuration actually in use, index integrity, and representative retrieval results.

## Model Behavior on Backend Failures

- Use `inspect_memory_state` and the reasons returned by the original operations to judge availability.
- When maintenance is needed, use `pal.self.maintenance` and the official Pal CLI.
- Do not fall back to directly editing the database or using informal scripts just because maintenance tools are missing.
- Verify unknown write results against the original `provider_id` / `mem_ref`; do not unilaterally switch to querying another backend, and do not evade errors by switching the active provider.
- Distinguish between "the CLI has accepted the job", "migration complete", "the running instance has adopted the new configuration", and "retrieval verification passed".

## Implementation Order and Acceptance

1. Inventory the current upgrade scripts, index maintenance, and provider lifecycle interfaces, and determine the owner invocation boundary of the official CLI.
2. Complete the CLI and migration job functionality before withdrawing the corresponding model tools, avoiding a maintenance capability gap.
3. Update `inspect_memory_state`, tool recovery prompts, built-in maintenance skills, CLI help, and related documentation, removing references to the old maintenance tools.
4. Verify that the model's native schema, search catalog, `read_tool`, and role tool projections no longer expose the above maintenance entry points.
5. Perform targeted verification of backend unavailability, migration interruption and recovery, embedding incompatibility, index rebuilds, and configuration activation paths; confirm that everyday memory reads/writes and original-reference verification still work.
6. Generate the tool inventory, counting the removed public maintenance entry points separately; full CI is handed to GitHub.

Implementation, copying installation files, activation on the running instance, and retrieval verification are recorded separately. When a resident implementation change is involved, activation is scheduled separately; editing configuration, rescanning, or attach must not substitute for an actual upgrade and code activation.

## Related Module Plans

- [Emotion broadcast and tool surface consolidation plan](pal_emotion_broadcast_plan.md)

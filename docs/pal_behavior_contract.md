# Retired behavior advisor

The behavior advisor plugin has been removed. It no longer publishes advice or
rule-management tools, registers scenario routes, or injects resident guidance.

Capability discovery belongs to execution; task affordances belong to tool
contracts and results. Channel protocols belong to their channel. Target-specific
operating constraints belong to the target owner. Durable preferences and past
decisions belong to memory, and reusable manuals belong to skill.

Skill owns its model, decorators, repository, and plugin lifecycle independently.
The existing `behavior_skills` table name is retained for stored-manual compatibility.
Skill commit/update no longer creates an additional behavior routing record.

On runtime startup, provisioning removes the obsolete managed behavior manifest.
Existing `behavior_affordances` records are left untouched for archival recovery;
they are not queried or projected. Historical L1 content is not rewritten.

See [advisor removal](advisor_removal.md) for backup and activation notes.

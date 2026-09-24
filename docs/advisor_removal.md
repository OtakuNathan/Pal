# Advisor removal

The behavior advisor plugin, route database access, advice/rule tools, resident
prompt providers, and core lifecycle callbacks have been removed. Execution's
capability discovery and result affordances remain. Skill is independent: its
model and decorators now live under `pal.skill`, and commit/update store manuals
without creating routing records. The `behavior_skills` SQL table name remains
unchanged so existing manuals survive the ownership move.

Bunshin's role instructions are not advisor rules. They are retained as a
`role_contract` developer section, without the old advisor header or routing
policy. Historical L1 content is not rewritten by this change.

## Backup

The pre-removal working tree, including uncommitted changes, was archived outside
the repository at:

`../pal-advisor-backups/20260924-024633/`

It contains `source-before-removal.tar.gz`, `working-tree.patch`, `HEAD`, and an
export of `behavior_affordances`. The runtime's original rows were not deleted.
Restore individual paths when needed; do not overwrite later work wholesale.

## Existing resident records

The local runtime contained four enabled records:

- Tool discovery reminder: superseded by System Map and tool contracts.
- Peer-channel reply rules: use the current channel-owned reply contract. The old
  record said to append a sentinel to every final; the current bridge treats an
  **entire** `[[peer_end]]` final as termination. Do not migrate that stale text
  verbatim. The normal final is already the peer reply and same-endpoint send is
  rejected by the channel capability.
- Local Pi USB boot-disk constraint: the user confirmed the underlying problem
  has been fixed and this rule is obsolete. It is archived only, not migrated
  into shell instructions.
- Astra postponement: retain as historical user preference/decision in memory,
  not a standing restriction overriding a new explicit request. The original
  record refers to case_f3f42d3777b1; its routing keywords were unrelated to its
  content. The archived record is retained for reconciliation.

This code change does not rewrite the live runtime's personal records. The historical
records above remain in the
original database and backup; they are not automatically converted into new
instructions or memories.

## External plugin compatibility

The native-shell setup manual previously imported the retired affordance decorator.
Its source and installed runtime copy now retain only the independently searchable
manual. Its publish/withdraw regression was updated and passed (4 tests, using the
installed native extension). OLED's unused behavior imports were removed from both
source and installed copy. Originals are in `external/` under the backup above.
No plugin was reattached; copied files are not proof of loaded changes.

## Activation

No running service was restarted or hot-reloaded during this change. Startup
provisioning removes the retired managed `plugins/_builtin/behavior/plugin.toml`
while leaving archived data alone. Core/skill changes require a host restart;
removing only a manifest is not proof that an already running advisor unloaded.
The obsolete local USB constraint does not need migration. New third-party manuals
import `skill` from
`pal.skill.decorators`; `pal.behavior` imports are no longer supported.

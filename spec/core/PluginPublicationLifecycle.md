# Plugin publication rollback

`PluginPublicationLifecycle.tla` models `PluginHost._attach_plugin` from scope
preparation through publication and rollback. Unlike the generic
runtime projection model, publication has observable staging: replay callbacks
look up the candidate in `generations`, so maps must exist before the fallible
binding, optional contribution replay, subscription activation, and attached
status steps finish.

- `generation` and `handleMap` represent candidate entries in `generations` and
  the applicable first-/third-party handle map
- `surface` represents live callable entry authority; dead registry metadata may
  remain discoverable until reconstruction succeeds
- `callbacks` represents other published callbacks that still require withdrawal;
  `resourcesClosed` records completion of resource cleanup
- `attached` represents lifecycle status, committed after subscription activation
- `pendingCleanup` represents unfinished rollback obligations, including failed
  surface withdrawal or import cleanup, not only scope callback count
- `owner` includes the local scope during preparation, retained early rollback
  ownership in `_pending_rollbacks`, and the later published generation. A scope
  may need cleanup even when `start` has not returned a module handle

A successful rollback removes only entries belonging to the failed candidate.
A failed rollback retains its generation/handle ownership and reports
`cleanup_failed`, allowing detach to retry while attach remains blocked. The
module-to-plugin routing map is discovery/lifecycle ownership metadata, also
used for attaching detached plugins; it is not a live generation map.

Early rollback failures retain the scope and original import metadata separately
from published generations. Attach/enable and dependent startup stay blocked;
detach, reload, disable and shutdown use the same retry path. Successful scope
callbacks are removed, so retries do not repeat them. Failed extension candidates
are retained by `ExecutionSlot` separately from installed extensions, with their
uninstall callback registered in the host scope before installation begins.

`Fail` revokes the failed mount's admission independently of registry compilation.
Withdrawal can then succeed or fail before resource cleanup starts. A failed
withdrawal retains the handle and resources, keeps admission closed, and never
reports the failed generation as attached on retry. `WithdrawSuccess` removes
remaining callbacks before cleanup is allowed. The withdrawal mutant closes
resources prematurely and must fail `CleanupAfterWithdrawal`.

Python cancellation is also a failure edge: async start/publication cancellation
rolls back and then propagates the original `CancelledError`; a cancelled cleanup
is retained for retry. Cancelled enable restores the previous enabled setting.
The abstract `Fail` action does not by itself establish these exception mappings;
the production regressions check them for first- and third-party plugins.

Run the safe model with `scripts/check_core_tla.sh /path/to/tla2tools.jar`.
The negative configuration `PluginPublicationLifecycleUnsafe.cfg` deliberately
retains maps after successful cleanup and must fail `PublicationAgrees`, tracing
`Stage -> Fail -> WithdrawSuccess -> CleanupSuccess`. Run it directly with TLC `-config` to check
the counterexample (now prefixed by `Start`).
`PluginPublicationLifecycleEarlyCleanupUnsafe.cfg` drops the early owner and
must fail `CleanupRetainsOwnership` on an early withdrawal or cleanup failure.
`tests/test_plugin_publication_rollback.py` exercises each
late failure point for both plugin sources, the real declared-skills replay
failure, successful retry, and retained cleanup ownership followed by retry.
It also exercises pre-handle startup failure, registration/publication failure,
shutdown retry and host-owned failed extension cleanup. These finite lifecycle
checks do not prove arbitrary callback termination or whole-program refinement.

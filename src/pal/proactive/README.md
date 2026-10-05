# proactive

Owns:
- proactive task definition and run semantics
- schedule boundary
- future output delivery target selection

Does not own:
- channel transport
- tool execution runtime
- worker lifecycle
- memory truth

Exposes:
- `ProactiveDefinition`
- `ProactiveTriggerEvent`
- `ProactiveManager`
- `ScheduleEngine`
- `ProactiveEventSource`

## Context and handoff

Each run uses a new model-cache scope and sees only its trigger and its own
active L1 turn. It does not consume queued user interjections or reuse the
resident history projection, including the projection's left-history bootstrap.
The ordinary codec sends the complete isolated request on every round.

Tool calls, results and output-snapshot references already belong to the shared
L1 store. Closing the turn makes these records available to later ordinary chat
requests; no summary-only handoff or duplicate transcript copy is needed.
Artifact ownership and file version/edit authority remain resident-owned.
File-read suppression additionally requires the source results to be visible in
the requesting model context, so another run's reads cannot hide file content.

A proactive run that exceeds its own context budget stops with its work recorded.
It must not compact the resident conversation to make space for an isolated
request. Independent proactive compaction is not implemented by this path.

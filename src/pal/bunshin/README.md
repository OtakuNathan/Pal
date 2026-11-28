# bunshin

Workflows hold a durable reference to their L3 generation. Roles, retries and
recovery borrow it read-only, including its embedding constructor. Writes within
the same generation remain visible; a swap retires that generation while existing
workflow and process references keep it alive. There are no workflow database
copies. Explicit forgetting uses the shared tombstone catalog. Memory candidates
continue through explicit review. Manager reconciles finished workflows and
interrupted registrations at startup and during its existing outbox lifecycle;
cleanup does not depend on Dreaming. Workflow creation runs in an owned worker
whose completion is joined through cancellation.

Workflow progress is reconstructed from the Task binding, workflow aggregates,
persisted graph generation, nodes and role receipts. The effect dispatcher
leases and acknowledges durable work. Node acceptance and recovery enter the
same graph scheduler. Workflow finalization packages the commit recorded for the
accepted sink with its bound verification reference and queues delivery to Pal.
Memory candidates follow the existing review path.

New Verifier assignments using `module_verification.v2` submit a verdict and
batch findings with counterexamples. Execution findings can cite a tool call ID.
Software PASS requires a successful run of the complete verifier suite on the
current candidate and corpus. At the sink the Verifier exercises the delivered
entrypoint across the system scenarios and cites the successful calls. A suite
that drives the public entrypoint can supply that evidence in one run. A failed
LSP diagnostic can be explained in the PASS note. Pinned v1 profiles and retries
retain their checklist and YAML report contract.

The explicit stopped-runtime cutover upgrades schema 29 or 30 to 31 in place and
preserves the database and checkpoint files. Ordinary repository opens require
the cutover first. Manager startup and cutover hold the same runtime ownership
lock. Keep pre-lock Managers stopped throughout the upgrade and drain live workers,
including old stdin-based questions, before replacing the host.
For older databases, archive cutover records the staged installation before
moving the old runtime. Rerunning setup after interruption resumes that operation.

The Manager web broker constructs only Execution, Web Search and Web Fetch.
Its shared runtime has no workflow or memory dependency. Runtime constructors
register resource cleanup before publishing modules and transfer ownership only
when construction succeeds. Shutdown closes web-request admission first, drains
already-admitted requests under the shared lifecycle gate, then closes the runtime.
Concurrent requests remain independent. Manager and web-runtime close operations
retain and join their cleanup tasks through repeated caller cancellation.
Cleanup retains unsuccessful actions for retry and never replays an already
successful resource closer. Role runtime cleanup uses the same owner and drains
execution calls before releasing its resources.

Package layout:
- `pal.bunshin` is the single implementation and public package; it has no
  parallel versioned implementation or compatibility import package
- `capabilities.py` owns Manager/plugin integration; `workflow_capabilities.py`
  owns public Task, Workflow, and catalog operations
- `catalog.py` owns editable profile/family overrides; `workflow_catalog.py`
  publishes immutable Family bindings for workflows
- `architecture_compilation.py` compiles the resources in
  `architecture_templates/` and `architecture_specializations/`
- `storage/`, `semantic_orchestration/`, and `runner_components/` retain their
  existing transaction, orchestration, and worker-runtime responsibilities
- the default worker entrypoint is `python -m pal.bunshin.worker_main`; formal
  models live in `spec/bunshin/`

All built-in roles, including Architect, run through the Pal worker and use
Pal's configured LLM endpoints and tool surface.
At the Manager boundary, `llm_admission` checks the role's pinned endpoint,
output budget and endpoint fingerprint before constructing Pal's
`EncodedTransportRequest`. The host-side `LLMProxy` streams it through
`DirectEndpointTransport` and settles usage through `LLMUsageLedger`.
Each request and its receipt belong to one run and attempt. Endpoint refresh
allows new requests to use the replacement while admitted streams drain.

`RunLedger` projects live progress for the frontend. A role session can reuse
its run ID across retries. Registration binds the current assignment and attempt.
A terminal event puts a live worker in `exiting` until its owner confirms
cleanup. The durable SQLite records support recovery across Manager restarts.
Recording a submission writes its artifact receipt and the attempt's submitted
state together. Applying the business action settles that exact assignment and
payload hash in the transaction that writes the aggregate, events and outbox.
A replay checks the same receipt against the same aggregate and outcome. A
recorded `ROLE_FAILED` is replayed through failure settlement without entering
the role wrapper, and cannot discharge a successful business action. Exact recorded
results are replayed before worker preparation, using the original prompt
binding rather than the current source or a new clone. The settlement wrapper
may still validate the execution workspace needed to publish its evidence.
Standalone reviews pin one isolated checkout and its scratch directory. Their
published evidence names that checkout's bound target, not the source
repository's latest HEAD, and collects files from the actual prompt workspace.

Sandboxed workers mount `llm/models`, `llm/providers`, and `llm/endpoints`
read-only so the shared LLM runtime loads the same hook configuration as Pal.
The worker uses the harness's resolved Python executable. Its executable,
versioned standard library and actual loaded `libpython` file are mounted
read-only, including relocated Linux installations. Shared-library discovery
uses the host process mappings rather than build-time `LIBDIR`; it does not
expose the rest of the interpreter prefix.

`start_workflow.execution_mode` defaults to `planned`. For software-engineering
`new_requirement` workflows, `direct` binds the complete task to one repository
Coder/Verifier pair and skips Architect, Architecture Reviewer, and architecture
approval. The Manager supplies a single-node graph solely as execution metadata;
no role authors an execution plan. Both roles receive the same immutable task
ledger and work view. See [direct mode](../../../docs/bunshin_direct_mode.md) for
inputs, reports, clarification, recovery, and activation.

Pal workers enqueue events into a bounded FIFO. One sender
task owns nonblocking JSON-line writes to stdout; model and tool execution do
not await progress delivery. A full queue applies asynchronous backpressure.
Approvals, clarifications, terminal events, and worker errors await pipe-write
confirmation (not a Manager persistence acknowledgement). Shutdown flushes
the queue with a bounded deadline and reaps the sender. Interrupted partial
frames cannot be followed by another frame on the same pipe; required delivery
failures fail the worker, and an unavailable stdout falls back to reporting the
original required message on stderr. Manager recovers structured failure and
retry metadata from these fallback records when stdout has no terminal event.

New workflow tools, SQL tables and role pack bindings use the Bunshin name.
The explicit v29 cutover renames the versioned tables and their
indexes, including FTS. Active tasks, artifacts and checkpoint bytes keep their
identities. Pinned packs normalize their legacy binding on read. Delivered review
buttons and stored interaction IDs continue to resolve. Role profile IDs and
versioned artifact protocols keep their published identities.

Software Coders can trace callers, tests and integration wiring across the bound
repository snapshot. Each parallel Coder works from its own module worktree.
A file-frozen interface is read-only. When an interface and its implementation
share a review-guarded file, the owning Coder may edit the body. The Verifier
checks public behavior against the approved declarations. In planned mode the
sandbox mounts the repository read-only and grants writes to assigned directories.
The role policy lives in `permissions.py`. It translates the assigned worktree,
protected paths, runtime resources and references into mounts for one invocation.
The sandbox launcher and file tools use this policy. Directory mount enforcement
is shared infrastructure in `pal.foundation.bwrap_permissions`.
Host launch code can supply `ReadOnlyBind` declarations through the launcher's
`runtime_mounts` argument. Destinations must be below the runtime's `data`
directory and disjoint from existing mounts. Role packs have no mount authority
through this interface. The role launcher uses it for the LSP Manager's runtime
directory when present. The host starts the LSP Manager and resolves its
language-server executables. The role connects to the mounted Unix socket.
When a Coder owns an individual file in a shared directory, its file tools send
the exact change to an attempt-local editor over an anonymous Unix socket.
Only that editor mounts the shared parent writable under `/tmp/pal/bunshin`
in its private namespace. It checks the pinned filename grant and replaces the
file atomically. The Coder sees the change at the original worktree path. The
process owner reaps the editor with the worker before releasing the worktree.
Verifier tests and immutable inputs remain read-only. Candidate publication
validates the complete delta against the assigned paths.

A software repair receives the complete RepairBill. Its checklist is optional.
On submission the Manager runs the whole local verifier suite in a fresh
read-only sandbox with private build scratch. It binds the passing result to
the candidate contents. An unsuccessful run
returns its output to the same Coder. Use ordinary pytest discovery for Python
corpora or a verifier-owned `run-tests.sh` in the bound corpus for native or
mixed projects. Known non-Python source files require that runner regardless
of the filenames, while ordinary data fixtures can accompany pytest. The script
runs from the repository root, reports all failures
and returns the suite exit status. The precheck has a 240-second execution budget
inside the five-minute submission RPC. A provider with only consumer-side tests or an older corpus
without a runnable suite goes to independent review with an explicit pending
precheck. Final acceptance always belongs to the Verifier.

Owns:
- durable workflow, aggregate, role-session, and worker-process orchestration
- Manager-compiled architecture templates, Family specializations, and
  role-TOML playbooks
- immutable `GraphIR` compilation, module views, and WorkItem ledgers
- producer/checker cycles for planning and for every executable graph node
- graph readiness, reverse finding propagation, replan reuse, and publication
  of the one authored terminal sink
- Bunshin v3 semantic work-checkpoint policy
- fresh runtime schema cutover and archive policy

Does not own:
- a second prompt compiler, turn executor, memory recall implementation, or
  tool facade
- channel transport
- capability canonical-path exposure to an LLM

Interaction rule:
- Architect authors one Family-specialized `architect.yaml`. `GraphCompiler`
  validates and compiles it to immutable `GraphIR` plus a source map; it never
  invents an integration, assembly, scenario, or system-verification node
- each Family pins a `graph_satellite.j2` alongside its schema and authoring
  template. That projection explains the Family's authored semantics to its
  execution domain and emits opaque per-node satellite data. Shared graph code
  only validates topology, persists/hash-compares satellites, and carries the
  common workspace policy; it does not interpret Family requirement, scenario,
  contract, or domain fields
- `GraphIR` is immutable node identity, contracts, and topology. Each
  `NodeCycle` owns its semantic producer/checker/repair cursor and exposes the
  next assignment's role and kind. `NodeSchedule` decides dependency readiness,
  fencing, and whether that cursor can retain a node slot. `NodeScheduler`
  materializes runnable work and its dependency baseline; it has no second
  persisted scheduling cursor. `GraphExecution` owns traversal, repair routing,
  replan reuse and overall completion; `WorkflowCoordinator` persists its
  facts and installs/activates the full graph generation. The immutable
  graph can exist while its Epoch remains `STARTING`; installation and the
  pinned preparation request (source epoch and contract) commit together.
  This prepared Epoch belongs to the current Workflow lineage even before
  active linkage. Parent pause/cancel fences compilation and activation;
  resuming replays the same preparation. Normal compilation and recovery
  commit complete membership, carried acceptance receipts, active workflow
  binding and predecessor retirement together. A Git
  carry may rebind checkpoint provenance only for the identical verified
  commit; a changed commit retains the worktree but must be checked again.
- existing `DagNodeRun` states are the durable effect progress and historical
  status projection (queued, process running, quiescing, snapshotting, receipt
  settled, control cleanup). They are not a separate decision about the next
  semantic role or dependency readiness. A settled operational receipt must
  match the accepted `NodeCycle` candidate; disagreement is an invariant
  failure, not permission for another scheduler to advance the graph.
  Recorder, event and outbox formats remain compatible. Workflow completion
  consumes the complete graph's exact accepted candidate/verification receipts
- a workflow pause is an admission fence, not permission to discard a
  committed result. The aggregate's pause request and new role receipt
  admission are fenced in the same SQLite write boundary. An already
  recorded result (including `ROLE_FAILED`) is consumed through its own
  settlement, not retired by pause. Architect, Producer and Verifier
  quiescence/snapshot phases drain while their admitted cycle cursor remains
  alive; the settled result is then paused at its next assignment boundary.
  A failed receipt enters triage; ordinary resume does not resolve that blocker.
  Dependency-repair capture keeps its existing assignment-cancellation boundary:
  freezing a node does not reject a receipt that wins the assignment transaction.
  Such a receipt belongs to the persistent cohort. Cancellation can still
  retire pending work through the exact assignment.
- mechanical control outbox effects are wake-ups for the current aggregate
  control intent. Workflow and epoch projection check that intent and write
  child requests in one transaction, with keys derived from current versions.
  A delayed pause, triage freeze or stale-assignment cleanup cannot retire a
  resumed assignment. An old wake-up for an intent that is current again may
  reconcile that new episode without replaying the old action identity.
- planning is Architect producer -> Architecture Reviewer checker -> Human.
  Every executable graph node, including the authored sink, is a Coder
  producer -> Verifier checker cycle. Software Coders run in parallel from
  accepted contracts; the sink Coder owns the source-level entrypoint and
  composition, while Manager waits for accepted dependency Candidates before
  assembling the sink checker worktree. The sink Verifier owns end-to-end,
  system, and delivery verification
- one module identity owns one reusable worktree and its Coder/Verifier logical
  sessions. The roles share the same Git baseline but have distinct writable
  developer/verifier test directories. Replan preserves the worktree and both
  sessions while name and responsibility stay stable; acceptance is carried
  only when the contract and incoming edges are also unchanged
- logical role sessions are durable data, not resident processes. A
  `RoleProcessShell` materializes an OS process only after acquiring capacity;
  the process owner reaps its child and closes registration, heartbeats and
  worktree occupancy before the next role can materialize. Manager grants node
  capacity. The graph scheduler retains it for the same runnable node across
  producer, verifier and repair attempts. A blocked dependency, pause, human
  wait, backoff or terminal node returns capacity to the shared pool
- Pal and Bunshin use the shared `core.AgentTurnRuntime`; Bunshin supplies only
  host ports, prompt fragments, tool observation hooks, and compaction policy
- `pal.compaction.continuity.v1` shares the resident summary policy: current
  context plus `constraints`, `state`, `decisions`, and `references`. Work
  continues from the persisted checklist, not a second summary task list.
  Compaction emits neither private chain-of-thought nor durable-memory candidates;
  eligible replay prefixes are isolated by the Bunshin logical scope
- Architect and Architecture Reviewer keep complete `task.yaml` authority.
  Manager renders one Family-specialized `architect.yaml`; Architect fills that
  form and submits it through the Manager's pinned schema validator. Reviewer
  submits structured findings. Coder and Module Verifier keep only their bound
  module view, local contract, code, and checklist authority
- the authored sink Coder still receives only its local module view. Manager
  projects whole-graph requirements, scenarios, and entrypoints into a separate
  `SystemDeliveryViewArtifact` visible only to the sink Verifier
- Bunshin Git reads use Manager-authenticated revisions. Coders may read all
  repository paths. Module reviewers inspect owned paths, local tests and
  dependency public contracts
- role profile fragments are playbooks, Manager-derived views are inputs, and
  the WorkItem checklist is the execution driver; none is a second contract
- every family uses the same Architect/Reviewer/Implementation/Verifier graph.
  Architect and Reviewer require profile participants; Implementation and
  Verifier are either both profile-backed or both explicit `null` participants
- role profile and execution harness are separate contracts. Each attempt
  pins the built-in Pal harness. Each role profile chooses its model and
  endpoint independently. Historical receipts keep their original harness
  identity. Drain any active external-harness sessions before upgrading
- the Bunshin compaction clock advances only for successful consumable LLM
  rounds. Provider errors, truncation, `compact_required`, tools, and
  compaction calls do not advance it
- output snapshots remain available while referenced by role L1 or active
  delivery/read pins; source-file edit authority belongs to delivered results
- evidence wrappers retain a tool's public response separately from private
  evidence and reuse that response verbatim, including on operation replay.
  They add only recording/reuse facts. Recording failures retain the lower
  result and distinguish an already-saved record from an uncertain write;
  they never reconstruct a public error from internal structured diagnostics
- sandbox evidence reads go through the role gateway, limited to references in
  the authenticated draft's recorded cases or the exact replayed operation.
  Public-result artifacts use the same byte-storage interface as stdout/stderr
- invocation directories and prepared role-workspace clones are disposable
  session-owned runtime state. A completed or cancelled session deletes them
  after its durable database transition; Manager startup reconciles terminal
  and orphaned spool while preserving active and suspended resumable sessions.
  Terminal workflow cleanup also retires workflow worktrees and artifact-module
  scratch; content-addressed artifacts, audit rows, and deliveries remain
- a new logical role session is durably `uninitialized` until its first
  checkpoint publication is acknowledged by Manager. Process claim/start,
  startup retry exhaustion, and prelaunch cancellation preserve that state.
  The worker writes its initial closed safe point, Manager publishes the
  encrypted file then commits initialization, and only an acknowledgement
  permits LLM/tool work. An interrupted file-before-database publication is
  restored on retry. Active/suspended sessions, including legacy records,
  require their checkpoint and fail closed if it is missing or corrupt; they
  never silently restart fresh. Successful legacy submissions also require
  continuation even if they did not publish a checkpoint
- checkpoint schema v8 stores a non-sensitive routing/fencing header plus one
  authenticated encrypted logical-process payload. Module-owned state is
  serialized behind that envelope; Manager routes it opaquely and only the
  target worker decrypts it. Older plaintext checkpoint schemas are rejected
- the encrypted coroutine payload also owns assignment-local artifact/evidence
  lists, memory candidates, and resource-budget counters. Native-process
  restart restores them only when the Manager input key is unchanged; a new
  semantic assignment mechanically starts those invocation-local fields empty
- checkpoints atomically replace the latest encrypted payload at every closed
  tool-protocol safe point, including role-stage entry, structured terminal
  boundaries, and cooperative restart. A hard crash therefore resumes from
  the latest closed boundary plus durable worktree, checklist, findings, and
  pending Manager input. On rebind or successful receipt replay, session storage
  first reconciles the latest retired PAL attempt's authenticated local output.
  A strictly newer closed point for that session, assignment and fence is
  selected in SQLite; a live or superseded attempt, missing selected checkpoint,
  and an unresolved interaction cannot authorize that promotion. The attempt
  file itself is only a candidate, not another recovery head.
  A role waiting for approval or clarification also
  checkpoints its exact tool-call cursor and completed prefix before exiting.
  Manager selects the encrypted envelope in the same SQLite transaction as
  the wait and its Task delivery. Closing the batch commits the replacement
  envelope and consumes the answer together. A replacement
  worker consumes the answer for the original call and continues the suffix.
  Ambiguous execution after an answer has been claimed requires reconciliation
  before replay. A replacement assignment can normalize the cancelled,
  unclaimed cursor of a superseded assignment; a claimed outcome needs a
  proven closed checkpoint. Pause and triage retain the claimed interaction
  while retiring its attempt. Pause adopts a proven closed boundary when available. The triage
  action can adopt a retired attempt's authenticated
  closed checkpoint and consume the answer in the same transaction, including
  when a successor aggregate reuses the same logical role session. Historical
  cancelled waits that do not own the selected cursor are left alone. Without
  a closed checkpoint the operator must check the tool effect before restarting
  the execution.
- closed tool calls and results enter L1 incrementally. Compaction reads only
  that frozen L1; role contracts, checklists, task fallback, and memory recall
  remain independently projected authority
- Bunshin uses the selected endpoint/model output limit by default. If a response
  reaches that limit, provider-committed tool items continue normally; otherwise
  the harness discards the uncommitted response and forces one bounded file,
  checklist, finding, question, or submit action instead of generic continuation
- compaction retires file authority owned by tool results it removes from L1.
  Output copies survive only when the replacement L1 retains their references
- Bunshin runtime databases older than v29 and valid SQLite databases without
  recognized schema metadata use the resumable archive cutover. Explicit
  profile and family overrides are copied after validation
- legacy workflow state, aliases, managed seed migrations, and plaintext
  checkpoint schemas through v7 are not migrated in place
- Manager records one origin/current delivery binding on the Task, outside
  `task.yaml`, `GraphIR`, workflow payloads, and worker context. Ordinary input
  from another channel never changes it; `rebind_bunshin_task_delivery` changes
  only that reply target. A dead target falls back to a connected recovery
  socket, otherwise the durable notification remains pending
- verified software delivery has one authority and one portable projection:
  Manager pins the accepted sink commit under `refs/bunshin/deliveries/*`, emits
  a content-addressed `git format-patch` from the captured source-snapshot
  commit, replays clean-Git patches with `git am --3way` and synthetic-snapshot
  patches with `git apply` in a temporary checkout, and requires
  the replayed tree to equal the verified tree. The patch is the default user
  delivery and terminal attachment. Workflow worktrees still retire; PRs and
  long-lived local checkouts are optional later publication/debug actions, not
  automatic completion side effects
- declared repo-relative input binding is one fail-closed contract in
  `pal.bunshin.input_binding`. Only reference entries explicitly declared
  repo-relative (a relative `path`, or any path explicitly marked
  `repo_relative: true`) are captured once per workflow into an immutable
  `InputBindingManifest` carrying the source repository HEAD commit and the
  plain SHA-256 of every input, with content stored durably as
  `BoundInputArtifact` blobs. Role workspaces without the repository
  materialize them deterministically at `inputs/<name>/<repo_path>` from the
  durable content — never from the mutable host repository — and re-verify the
  same hashes on every retry, restart, or fenced attempt; repository-including
  workspaces verify the in-repo paths instead. Repo-relative declarations are
  handled exclusively by this binding; ordinary references use host paths.
  Recovery of existing prompt packs removes obsolete host aliases for declared
  inputs and asks the role to recheck any decisions based on those aliases.
  An assignment retry chooses its retained prompt workspace before binding
  inputs or preparing the environment. The subsequent harness binding must
  consume that same prompt and workspace. Repository files are verified in
  place, artifact-style inputs are restored there from durable content, and
  ongoing role work is preserved rather than replaced by a new clone.
  Manager semantic reference names remain fixed. Declared inputs receive a
  deterministic distinct alias if their normalized names collide, and both
  initial and resumed prompts use the same allocation rule. The original
  declaration name stays recorded on the input projection.
  The Manager-owned `inputs`
  workspace root is excluded from candidate snapshots, tree fingerprints, and
  deliverables solely by the first-path-component predicate
  `is_bound_input_path`; worker-authored paths that merely contain an `inputs`
  component elsewhere are ordinary content and are never excluded

## Public workflow control

`control_bunshin_workflow(task?, operation, reason?)` provides `pause`, `resume`,
`cancel`, and `archive`. The task selector keeps its existing unambiguous-Task
resolution; archive includes terminal workflows, the other operations do not.
Pause/cancel remain asynchronous requests. Resume retains paused-workflow recovery
and orphaned-work normalization into triage; it does not resolve blockers.
Archive requires a completed/rejected/cancelled workflow, preserves its Task, and
reports no effect when already archived. Internal owner methods and state-machine
transitions are unchanged. The old public resume/archive tool aliases are retired;
the former public `command` parameter is replaced by `operation`.

`restart_bunshin_execution` and `resolve_bunshin_triage` remain separate, as do
architecture decisions and Architect answers. `start_bunshin_workflow` remains
direct. After a plan is cancelled before acceptance, its Task can start fresh
planning with `restart_cancelled_plan=true`. The new workflow uses the previous
goal, durable task ledger, and frozen repository-relative inputs. The new
planning pass captures the current source tree. In Git workspaces, both plan
recovery and planned execution restart expose frozen inputs through `/pal/references/`
from workflow-owned runtime storage. Artifact workspaces materialize them under `inputs/`.
`rebind_bunshin_task_delivery` is now indirect: discover/read its contract
and invoke through `call_tool`; its destination selection and Manager binding
validation are unchanged. Main Pal now has 11 authored Bunshin aliases with one
direct tool. The 36 role-only aliases are outside this change.

`BunshinEventPort` covers event readiness, draining, delivery eligibility,
accepted-part lookup, and acknowledgements.
The handler submits `ControlDelivery` directly through `CORE_CONTROL` and
acknowledges each accepted part before settling the event. Failed channel or
manager acknowledgements preserve the pending event and accepted-part ledger.
Manager startup reads prompt logging from the Core snapshot; subsequent changes
arrive through the declared observer. Wakeups use Core's public interface.


## General background tasks and managed Git

Direct workflows pin an externally supplied task plan and independent role
bindings. General defaults to no independent verifier; SWE retains its default
verifier. A null checker settles the actual candidate with an explicit
not-required receipt. A null producer waits for a real external product
instead of assigning a worker. Planned workflows can skip independent plan
review while preserving human approval.

General Tasks may choose `workspace.kind=managed_git`. The canonical checkout
is `data/bunshin/general/<semantic-task-name>`, or `proactive/` for the reserved
scheduled Task. Workers edit isolated Git worktrees. A short shared/exclusive
I/O gate serializes foreground local I/O against source capture and promotion.
Publication captures foreground changes, merges disjoint path changes and
preserves both commits on a conflict. The receipt distinguishes the accepted
candidate commit from the promoted canonical commit.

General Direct submission accepts `result_text`, `next_steps`, and
`deliverable_paths`; actual files are extracted from the accepted Git commit.
No independent verification is represented as not required. The ordinary
SWE patch delivery contract remains available.

The durable background inbox carries General and scheduled terminal results to
Resident independently of its active conversation. Channel confirmation and
resident consumption have separate records. The runtime upgrade is an offline
v31-to-v32 migration, including proactive business-data import and compatibility
links. Perform it using `pal setup --upgrade --runtime-root <dir>` with the host
and manager stopped. A resident host restart is required for the new handoff
path.

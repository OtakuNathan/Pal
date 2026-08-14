# Pal Public Proof Demo

This is a 3–5 minute recorded demo of Pal as a persistent personal agent
runtime. Bunshin appears only when the job outgrows a direct turn. The demo is
designed around observable state and an exported evidence bundle rather than a
carefully selected model transcript.

The complete workflow may take longer than five minutes depending on the LLM
provider. Waiting periods should be cut or accelerated in a recording; no
workflow transition should be fabricated.

## What the demo proves

The demo has one protagonist and four observable boundaries:

```text
person -> channel -> Pal -> direct turn
                         -> durable Bunshin workflow
                                      -> reviewed artifact
channel <- Pal delivery <-------------+
```

1. **One Pal, several surfaces.** The daemon remains the owner of the turn;
   socket, Telegram, and the desktop avatar are transports and presentations.
2. **Cheap conversation, structured delegation.** A direct request stays in
   the ordinary turn loop. A larger request can become a contract graph with
   role-specific work and review.
3. **Process death is not task death.** A Pal service restart ends the resident
   processes. Durable workflow state lets the new Manager reclaim unfinished
   work with a new attempt and fencing token.
4. **Delivery returns to the parent.** Workers do not become new chat agents.
   The reviewed result is delivered by Pal through the task's bound route.

## Facts and claims

Use the wording in this table when narrating the demo.

| Statement | Classification | Verification |
| --- | --- | --- |
| Pal runs as a supervised daemon and accepts local socket messages. | Verifiable fact | `systemctl --user status pal` and `pal client` |
| Channel providers and Bunshin run behind lifecycle-owned boundaries. | Verifiable fact | process tree, provider manifests, runtime inspection |
| Bunshin persists tasks, aggregate events, role assignments, attempts, and delivery records. | Verifiable fact | exported `pal.public-proof.v1` bundle |
| A restarted assignment receives a later attempt and fencing token. | Verifiable fact | proof bundle `recoveries` and `attempts` |
| Referenced artifacts are content addressed and intact. | Verifiable fact | typed SHA-256 verification performed by the capture script |
| Pal is more reliable, more personal, or better than another agent. | Marketing claim until measured | requires a published benchmark or user study |
| Every capability is a plugin. | Marketing shorthand | Core runtime boundaries are deliberately not plugins |
| Pal never loses work. | Invalid absolute claim | hardware, storage, provider, and operator failures still exist |

## Recording script

### 0:00–0:35 — Meet the same Pal in two places

Show the desktop avatar or Telegram next to a terminal. Do one small direct
turn, then connect the local recovery channel:

```bash
pal tty --runtime-root ~/.pal
```

Narration: “The character is a presentation. The long-running Pal process owns
the conversation, memory, capabilities, and delivery.” Do not show private
history or configuration files.

### 0:35–1:20 — Delegate a job, not the identity

Give Pal a bounded task that produces an artifact and explicitly request
Bunshin. The repository's real dogfood run used this shape:

```text
Use Bunshin to review the bound project documents and produce a short public
proof report. Separate verifiable facts from marketing claims, include a
reproducible failure-recovery demo, write only a Bunshin artifact, and preserve
the completed workflow for evidence.
```

Show Pal creating the task and then ask it for the task status. Point out the
Task id, Workflow id, current phase, graph nodes, and dependency blockers. The
exact model prose is not evidence; these identifiers and states are.

### 1:20–2:15 — Show contracts and role boundaries

Open the architecture review card. Show only:

- the requirement-to-module mapping;
- graph dependencies;
- the read/write constraint;
- the planned reviewer/verifier boundary.

Accept the architecture through the normal Pal interaction. Do not update the
database by hand. Show one node enter `PRODUCING` while its dependants remain
`BLOCKED_BY_DEPS`.

### 2:15–3:05 — Kill the resident process

While a worker is active, record its current attempt and fencing token through
Pal's status surface or a proof capture. Then restart the service:

```bash
systemctl --user restart pal
```

Reconnect with the TTY after the socket returns. Ask Pal for the same Task by
name. Show that the Workflow id is unchanged and the workflow is active again.
The expected mechanical evidence is:

```text
attempt 1, fence 1 -> lost/suspended during restart
attempt 2, fence 2 -> running/completed after recovery
```

The exact attempt numbers may differ in a later run. The required invariant is
that ownership advances and the stale attempt does not regain authority.

### 3:05–4:15 — Close the loop

Show the final artifact and its reviewer/verifier outcome arriving through Pal.
Then export the durable record:

```bash
python scripts/capture_public_proof.py \
  --runtime-root ~/.pal \
  --workflow-id <workflow-id> \
  --repo . \
  --expected-repo-head <commit-sha> \
  --include-task-text \
  --output-dir /tmp/pal-public-proof
```

`--include-task-text` is appropriate here only because the demo Task is
deliberately public. Omit it for ordinary Tasks; the exporter emits hashes of
the title and objective instead.

Open `/tmp/pal-public-proof/proof.md`. The capture passes only when:

- aggregate event versions are contiguous;
- referenced typed artifact hashes verify;
- a multi-attempt recovery with monotonic fencing is present;
- the expected repository commit still matches; and
- tracked files in the operator worktree are clean.

The companion `proof.json` contains the redacted timeline for independent
inspection. It intentionally excludes prompt text, provider credentials,
artifact bodies, filesystem storage paths, and raw error messages.

### 4:15–4:40 — End on the product, not the machinery

Narration:

> Pal stayed the same agent before and after the process restart. Bunshin was
> the temporary execution organization it unfolded for one substantial job;
> the result, memory, and relationship still returned to Pal.

## Reproduction notes

- Linux and `bubblewrap` are required for the complete Bunshin path.
- Use a disposable or deliberately public task. The proof exporter is
  metadata-only, but the live UI may contain private conversation text.
- Record the repository commit before starting the workflow.
- Do not delete completed Bunshin records until after evidence capture.
- Service restart is deliberate fault injection. Warn anyone using a live
  Telegram or desktop channel that it will disconnect briefly.
- Keep the raw screen recording and generated proof bundle together. The video
  communicates the experience; the bundle supports the technical claims.

## Included dogfood evidence

The repository contains one real capture under
[`docs/evidence/2026-08-14-public-proof`](evidence/2026-08-14-public-proof/proof.md).
It was initiated through Pal's socket channel against the live Pal repository.
During Architecture Reviewer execution, the complete Pal service was restarted.
The old attempt was recorded as lost and the same durable assignment resumed
under a second attempt with a newer fencing token.

That first run is deliberately not presented as a happy-path delivery. Its
`general` artifact workspace did not receive the three named source documents
as bound inputs. The worker emitted a blocker instead of fabricating a review;
Verifier entered triage, and the operator cancelled the workflow without retry
or cleanup. The evidence directory keeps both the successful recovery
checkpoint and the terminal failure record. Fix or avoid that input-binding
gap before recording the final public happy path.

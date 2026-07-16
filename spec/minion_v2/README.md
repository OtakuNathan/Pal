# Minion V2 state-machine models

These specifications model the domain-independent orchestration contract before
the Python worker spine implements it.

- `ModuleLifecycle.tla` models one durable Node with long-lived Coder and
  Verifier role coroutines. A role may yield only after Manager records and
  settles its result receipt.
- `DagLifecycle.tla` models dependency readiness, graph-wide pause/cancel, and
  architecture-defect freeze/replan propagation.

The models intentionally abstract prompts, artifact contents, Git, and provider
details. Those are values carried by transitions, not additional lifecycle
owners.

Run TLC with a pinned `tla2tools.jar`:

```bash
java -XX:+UseParallelGC -jar /path/to/tla2tools.jar \
  -config spec/minion_v2/ModuleLifecycle.cfg \
  spec/minion_v2/ModuleLifecycle.tla

java -XX:+UseParallelGC -jar /path/to/tla2tools.jar \
  -config spec/minion_v2/DagLifecycle.cfg \
  spec/minion_v2/DagLifecycle.tla
```

TLC proves the abstract protocol. Python transition-table conformance and
SQLite/outbox crash-window tests remain required because model correctness does
not imply implementation correctness.

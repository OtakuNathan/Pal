# Core service contracts

Core coordinates semantic effects. LLM owns generation, request projection and
compaction execution. Memory owns history capture, installation and L3 maintenance.

## Generation and metadata

`LLMService` is the published service contract. `metadata()` returns typed
endpoint facts; there is no parallel Query port or signature negotiation.
`generate()` exposes one asynchronous semantic response stream. A complete HTTP
response is one terminal update; streaming adds semantic deltas before it.
Transport framing and endpoint response mode stay inside LLM.

The terminal update carries a call-local `LLMGenerationResult`, including its
endpoint, model and projection receipt. Core does not construct results from
shared `last_*` inspection telemetry. Concurrent resident and BTW requests use
the same LLM instance and transport with separate request facts and projection scopes.

## Compaction and maintenance

`CompactionOperation` coordinates the transaction through `CompactionService`
without implementing provider strategies or constructing execution limits. Memory captures the frozen left segment. LLM prepares native or
cached request material, executes bounded attempts and returns a validated
candidate. Memory stages the archive and installs the candidate atomically.
Core then coordinates checkpointing, projection rebase and result retirement.
A post-commit fault is classified from Memory's committed record and does not
turn a successful installation into a claim that history is unchanged.

Sleep first takes Core's admission barrier. Memory then fences its selected L3
repository, performs maintenance and mounts the published generation. Freeze
failure releases admission; a published generation that cannot be mounted keeps
admission closed. Backend and repository differences remain in Memory.

## Publication and execution

Consumers subscribe once with `PortRegistry.observe(key)`. Publication validates
the candidate and pushes the new owner into `ServiceRef.current`. Reading the
reference requires no registry lookup or generation comparison. In-flight
operations retain their captured owner. Withdrawal checks owner identity so an
old generation cannot remove its replacement.

Tool lifecycle access is declared in immutable `ToolExecutionSemantics` as
shared, exclusive or owner managed. Aliases do not select locks. Registered
calls capture the binding and its policy together; a queued call cannot adopt
a replacement binding after obtaining a different generation's lock.
The generic cancellation-safe gate belongs to Foundation. L3 provider selection
and registration belong to Memory rather than Execution.

## Temporary side questions

BTW borrows an immutable closed L1 prefix and keeps a private tail. Its
restriction and question are appended together as the last new user message.
Existing instructions and tool schemas remain unchanged. Temporary execution
visibility and cleanup use Execution's explicit request-context boundary.

Internal Python interfaces change together with their consumers. Retired Core
and Plugins package exports, Query and side-runtime factories are removed.
Persistent configuration, archives and recovery formats are preserved. This
checkout requires an external host restart for resident activation.

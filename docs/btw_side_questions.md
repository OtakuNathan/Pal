# Temporary side questions

`/btw <question>` starts one temporary, one-shot question alongside the resident conversation.
The command is handled before ordinary message admission: it neither waits in
the chat queue nor becomes an interjection. Multiline questions and quotes are
preserved. An empty command returns usage; a second side question returns busy
until the existing one ends.

The side turn sees a fixed snapshot of the closed **L** prefix. It does not see
the remaining **R** tail, including recent main-task input or results that have
not crossed that boundary. An empty L still permits a standalone question.
Normal conversation and compaction can proceed while the snapshot is in use.
Automatic live execution updates are not injected into the side question and
remain available for delivery to the main conversation.

The additional user message instructs the model to answer directly where
possible and perform only read-only operations. File, configuration and runtime
changes, message sending, and other write side effects are forbidden by that
instruction. Tool definitions and permissions are the same as the main session;
this mode uses model instructions rather than an execution sandbox.
The instruction also forbids background tasks, long-lived processes and shell
sessions requiring later management. Shell queries must finish in this request;
there is no session handoff to the main conversation. Read-only tools may still
require multiple model requests to produce this one answer.

The request retains the resident cache identity and public prefix. A detached
projection preserves the captured prefix's native wire material while owning
its own generation state. Additional instructions live in the new user message,
not in system instructions or a reduced tool schema. Usage is accounted normally;
side turns do not replace resident continuation state or cache-warming anchors.
Provider cache hits still depend on the matching prefix and provider policy.
LLM implementations expose this lifecycle through the optional
`llm:side_turns` port; ordinary providers without that port keep their existing
contract and return an unavailable-feature explanation for `/btw`.

Input, reasoning, tool results and replies live only in the side turn's temporary
context. They are not appended to resident L1, searchable conversation history,
or a recoverable turn. Ordinary channel delivery retains its transport semantics.
Replies and stream updates use the route that opened `/btw`. Tool state and
snapshot leases are released when it ends; captured files remain readable even
if the resident compacts meanwhile.
Native shell sessions created by this turn have temporary ownership: automatic
observations and completion turns cannot deliver them into resident history.
Reading an existing main-owned session or retained output only borrows its
contents. It does not advance the main conversation's delivery position,
acknowledge its completion, or release its output, including on read failure
or cancellation of the side question.
Before the side turn ends, its execution owner terminates and releases remaining
sessions it owns, without stopping main-owned work. Cancellation during cleanup
waits for that cleanup to exit before releasing the side slot and snapshot leases.
Uncertain remote cleanup retains the original operation tickets and reports a
cleanup failure. When the backend becomes reachable again, its owner queries
those tickets and retires late sessions and output without resubmitting commands
or opening another model conversation. A later `/btw` always starts a new question.

The side turn emits ordinary start/end observations with `turn_kind=btw` and an
independent turn ID. `/interrupt` continues to target the main turn; v1 adds no
separate side-turn cancellation command. Reset and host shutdown cancel the side
turn. Sleep and shutdown admission rules still apply. Context overflow ends the
side question with an explanation rather than compacting resident history.

Activation of this Core implementation requires an external full Pal restart.
The native execution plugin must also include the temporary-session lifecycle
changes; updating Core alone does not update an installed plugin.

"""Model-facing contracts for native shell execution."""
from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from pal.execution.shell_exec import SHELL_EXEC_GUIDANCE
from pal.execution.tool_facade import NextToolHint, StrictToolModel, ToolAffordance, ToolGuidance

from .adapter import TERMINAL


RUN_GUIDANCE = SHELL_EXEC_GUIDANCE.model_copy(update={
    "purpose": "Run a shell command; return its result or a live session for continued execution.",
    "use_when": SHELL_EXEC_GUIDANCE.use_when + (
        " wait_ms is the response wait (default five minutes, one second for a PTY), not a kill deadline."
        " timeout_ms is an optional hard deadline. A nonzero session_id means the command continues;"
        " do not run it again. Completion is delivered separately; avoid repeated short polling."
        " Set tty=true when the program needs interactive terminal input."
        " Command success requires status=exited and returncode=0; a returned snapshot alone does not mean success."
    ),
    "failure_next_steps": (
        "Inspect status, returncode, stdout and stderr before deciding whether a command is safe to repeat."
        " A live session is not a failure: follow its session affordances. If output delivery fails,"
        " recover the retained output through the host; do not repeat the command to retrieve output."
        " A missing or retired session does not prove that its command never ran."
    ),
    "next_tool_hints": (NextToolHint(
        name="shell_session",
        use_when="A returned session needs a snapshot, PTY input/resize, explicit termination, or completed-output release.",
    ),),
})

SESSION_GUIDANCE = ToolGuidance(
    purpose="Inspect or control an existing native shell session without rerunning its command.",
    use_when=(
        "Use the session_id returned by the shell. read returns a full snapshot, not a consuming byte stream;"
        " wait_ms can wait for exit (default zero, maximum five minutes). Completion arrives separately,"
        " so read when progress or an interactive prompt matters, not in a short polling loop."
        " write queues exact PTY text (include a newline to submit); acceptance is not proof it was processed."
        " resize changes a live PTY. terminate requests cancellation; wait for terminal status to confirm exit."
        " release explicitly discards a completed session's retained output. Successfully delivered terminal"
        " output is released automatically and remains readable through any returned result_handle."
    ),
    do_not_use_when=(
        "Do not invent session IDs or use zero. Do not write or resize a non-PTY session."
        " Do not release a running session; terminate it first if stopping is intended."
        " Use read_tool_result to read paged output already delivered."
    ),
    failure_next_steps=(
        "For invalid_session, consult the previous result/result_handle; reset or consumption may have retired it."
        " Do not automatically rerun its command. For a live-session precondition error, read its current status."
        " After uncertain input delivery, inspect output before resending non-idempotent input."
    ),
)


class RunInput(StrictToolModel):
    cmd: str = Field(min_length=1)
    cwd: str = ""
    tty: bool = False
    wait_ms: int | None = Field(default=None, ge=0, le=300000)
    timeout_ms: int | None = Field(default=None, ge=1, le=2147483647)


class SessionInput(StrictToolModel):
    session_id: int = Field(gt=0, le=9223372036854775807)
    action: Literal["read", "write", "resize", "terminate", "release"] = "read"
    wait_ms: int | None = Field(default=None, ge=0, le=300000)
    text: str | None = None
    rows: int | None = Field(default=None, ge=1, le=65535)
    columns: int | None = Field(default=None, ge=1, le=65535)

    @model_validator(mode="after")
    def validate_action_arguments(self):
        required = {"write": {"text"}, "resize": {"rows", "columns"}}.get(self.action, set())
        allowed = required | ({"wait_ms"} if self.action == "read" else set())
        supplied = {name for name in ("wait_ms", "text", "rows", "columns") if getattr(self, name) is not None}
        if not required <= supplied or not supplied <= allowed:
            raise ValueError(f"{self.action} requires {sorted(required)} and only accepts {sorted(allowed)}")
        if self.text is not None and len(self.text.encode("utf-8")) > 65536:
            raise ValueError("PTY input must fit the 64 KiB input queue")
        return self


def session_affordances(result: dict) -> list[ToolAffordance]:
    sid = result.get("session_id", 0)
    status = result.get("status")
    if not sid or status in TERMINAL or status in {"released", "notification_retry_queued"}:
        return []
    actions = [ToolAffordance(
        tool="call_tool", arguments={"name": "shell_session", "args": {"session_id": sid, "action": "read", "wait_ms": 300000}},
        reason=f"Session {sid} is {status}, not a command failure. Read/wait when needed; do not rerun or busy-poll.",
    )]
    if result.get("tty") and status != "terminating":
        actions.append(ToolAffordance(
            tool="read_tool", arguments={"name": "shell_session"},
            reason=f"Session {sid} has a PTY. Discover write/resize arguments if it needs input or terminal resizing.",
        ))
    if status != "terminating":
        actions.append(ToolAffordance(
            tool="call_tool", arguments={"name": "shell_session", "args": {"session_id": sid, "action": "terminate"}},
            reason="Only if stopping this command is intended; this requests cancellation, not an immediate exit guarantee.",
        ))
    return actions

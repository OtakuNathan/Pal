from __future__ import annotations

from enum import StrEnum


class EventKind(StrEnum):
    USER_MESSAGE = "user.message"
    REPLY_DELIVERED = "reply.delivered"
    REPLY_FAILED = "reply.failed"
    PROACTIVE_TRIGGER = "proactive.trigger"
    MINION_PROGRESS = "minion.progress"
    MINION_TERMINAL = "minion.terminal"
    MINION_STANDALONE_REVIEW_COMPLETED = "minion.standalone_review_completed"
    MINION_CLARIFICATION_REQUEST = "minion.clarification_request"
    MINION_CLARIFICATION_RESOLVED = "minion.clarification_resolved"
    MINION_ARCHITECTURE_REVIEW_PENDING = "minion.architecture_review_pending"
    MINION_ARCHITECTURE_REVIEW_RESOLVED = "minion.architecture_review_resolved"
    CONTROL_ACTION = "control.action"
    SLASH_COMMAND = "slash_command"
    INTERACTION_RESULT = "interaction_result"
    APPROVAL_REQUEST = "approval_request"


class SourceKind(StrEnum):
    CHANNEL = "channel"
    PROACTIVE = "proactive"
    MINION = "minion"
    CONTROL = "control"


class EffectKind(StrEnum):
    LLM_PREFLIGHT = "llm.preflight"
    LLM_REQUEST = "llm.request"
    TOOL_CALL = "tool.call"
    MEMORY_COMPACT = "memory.compact"
    MAILBOX_REPLY = "mailbox.reply"
    MAILBOX_REPLY_STREAM = "mailbox.reply.stream"


class RuntimeStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    QUEUED = "queued"
    UNSUPPORTED = "unsupported"
    INVALID = "invalid"
    NOT_FOUND = "not_found"
    RETRY = "retry"
    SKIPPED = "skipped"
    FORBIDDEN = "forbidden"


class LLMPreflightStatus(StrEnum):
    READY = "ready"
    COMPACT_REQUIRED = "compact_required"


class GuardStatus(StrEnum):
    OK = "ok"
    REPEAT_STAGNATION = "repeat_stagnation"
    OSCILLATION_STAGNATION = "oscillation_stagnation"


class GuardAction(StrEnum):
    CONTINUE = "continue"
    TERMINATE_TOOL_LOOP = "terminate_tool_loop"


class LLMFinishReason(StrEnum):
    STOP = "stop"
    TOOL_CALLS = "tool_calls"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    STUB = "stub"
    FALLBACK = "fallback"
    COMPACT_REQUIRED = "compact_required"


class LLMResponseMode(StrEnum):
    CHAT = "chat"
    OPERATIONAL = "operational"
    REVIEW = "review"


class ChannelStreamUpdateKind(StrEnum):
    TEXT_DELTA = "text_delta"
    REASONING_DELTA = "reasoning_delta"
    TOOL_CALL = "op_tool_call"
    PROGRESS = "progress"
    DONE = "done"
    ERROR = "error"
    COMPACT_REQUIRED = "compact_required"

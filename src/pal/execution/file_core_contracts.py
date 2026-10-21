"""Standard-library boundary types for the exported standalone file engine.

The resident engine binds to Pal's richer facade types. Workers serialize
these values and the host reconstructs its own facade objects at the boundary.
No filesystem or authorization behavior belongs in this adapter.
"""
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RuntimeStatus(StrEnum):
    OK = 'ok'
    ERROR = 'error'
    INVALID = 'invalid'
    FORBIDDEN = 'forbidden'
    NOT_FOUND = 'not_found'


class EffectOutcome(StrEnum):
    NONE = 'none'
    NOT_STARTED = 'not_started'
    NOT_APPLIED = 'not_applied'
    APPLIED = 'applied'
    UNKNOWN = 'unknown'


class RetryDirective(StrEnum):
    DO_NOT_RETRY = 'do_not_retry'


@dataclass(frozen=True)
class EffectReceipt:
    outcome: EffectOutcome
    receipt: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityResult:
    status: str
    llm_text: str
    text: str = ''
    structured: dict[str, Any] | None = None
    effect_receipt: EffectReceipt | None = None
    recovery_hint: str = ''
    context_delivery: dict[str, Any] | None = None


class ToolExecutionError(RuntimeError):
    def __init__(self, message, *, error_code, retry=None, effect_receipt=None,
                 details=None, recovery_hint=''):
        super().__init__(message)
        self.error_code = error_code
        self.retry = retry
        self.effect_receipt = effect_receipt
        self.details = details or {}
        self.recovery_hint = recovery_hint

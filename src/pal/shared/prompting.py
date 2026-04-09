from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pal.foundation import EventEnvelope


@dataclass(frozen=True)
class PromptFragment:
    section: str
    title: str
    content: str
    priority: int = 100
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptIRBlock:
    block_id: str
    title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptIR:
    system_blocks: tuple[PromptIRBlock, ...] = ()
    user_context_blocks: tuple[PromptIRBlock, ...] = ()
    primary_input: str = ""
    turn_kind: str = "chat"


@dataclass(frozen=True)
class PromptAssemblyContext:
    event: EventEnvelope | None = None
    core_mode: str = "default"
    turn_kind: str = "chat"
    task_id: str | None = None
    work_order_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class PromptFragmentProvider(Protocol):
    provider_id: str
    module_id: str

    def build_prompt_fragments(self, context: PromptAssemblyContext) -> list[PromptFragment]:
        ...

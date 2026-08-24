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

    def __post_init__(self) -> None:
        target = str((self.metadata or {}).get("prompt_target") or "").strip().lower()
        if not target:
            raise ValueError("prompt fragment must declare metadata.prompt_target")
        if target not in {"system", "developer", "user_context", "runtime_reminder"}:
            raise ValueError(f"unknown prompt fragment target: {target!r}")
        object.__setattr__(
            self,
            "metadata",
            {**dict(self.metadata or {}), "prompt_target": target},
        )


@dataclass(frozen=True)
class PromptIRBlock:
    block_id: str
    title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromptIR:
    system_blocks: tuple[PromptIRBlock, ...] = ()
    developer_blocks: tuple[PromptIRBlock, ...] = ()
    user_context_blocks: tuple[PromptIRBlock, ...] = ()
    runtime_reminder_blocks: tuple[PromptIRBlock, ...] = ()
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

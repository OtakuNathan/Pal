from __future__ import annotations

from pal.shared.tool_protocol import ToolDefinitionIR

import importlib.util
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Callable, Iterable, Mapping

from pal.llm.ir import (
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
    TextPartIR,
)


class ModelHookError(RuntimeError):
    pass


MessageHook = Callable[[tuple[LLMMessageIR, ...]], Iterable[LLMMessageIR] | None]
ToolHook = Callable[[tuple[ToolDefinitionIR, ...]], Iterable[ToolDefinitionIR] | None]


@dataclass(frozen=True)
class ModelHook:
    model_id: str
    developer_instructions: tuple[str, ...] = ()
    adjust_messages: MessageHook | None = None
    adjust_tools: ToolHook | None = None

    def __post_init__(self) -> None:
        if not str(self.model_id or "").strip():
            raise ValueError("model hook model_id must be non-empty")
        object.__setattr__(
            self,
            "developer_instructions",
            tuple(str(item).strip() for item in self.developer_instructions if str(item).strip()),
        )

    def apply(self, request: LLMRequestIR) -> LLMRequestIR:
        messages = list(_apply_messages(self.adjust_messages, request.messages))
        insertion = next(
            (index for index, message in enumerate(messages) if message.role not in {MessageRole.SYSTEM, MessageRole.DEVELOPER}),
            len(messages),
        )
        hook_messages = [
            LLMMessageIR(
                role=MessageRole.DEVELOPER,
                parts=(TextPartIR(text),),
                semantic_kind="model_hook",
            )
            for text in self.developer_instructions
        ]
        messages[insertion:insertion] = hook_messages
        tools = _apply_tools(self.adjust_tools, request.tools)
        return replace(request, messages=tuple(messages), tools=tools)


@dataclass(frozen=True)
class ModelHookRegistry:
    hooks: Mapping[str, ModelHook]

    def __post_init__(self) -> None:
        object.__setattr__(self, "hooks", MappingProxyType(dict(self.hooks)))

    @classmethod
    def load(cls, runtime_root: Path) -> "ModelHookRegistry":
        root = Path(runtime_root) / "llm" / "models"
        hooks: dict[str, ModelHook] = {}
        if not root.is_dir():
            return cls(hooks)
        for path in sorted(root.rglob("*.py")):
            if path.name.startswith("_"):
                continue
            hook = _load_hook(path)
            if hook.model_id in hooks:
                raise ModelHookError(
                    f"duplicate model hook for exact model_id={hook.model_id}: {path}"
                )
            hooks[hook.model_id] = hook
        return cls(hooks)

    def apply(self, model_id: str, request: LLMRequestIR) -> LLMRequestIR:
        hook = self.hooks.get(str(model_id))
        return hook.apply(request) if hook is not None else request


def _load_hook(path: Path) -> ModelHook:
    module_name = f"pal_runtime_model_hook_{abs(hash(path.resolve()))}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ModelHookError(f"cannot load model hook: {path}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise ModelHookError(f"model hook failed to import: {path}: {exc}") from exc
    return _hook_from_module(module, path)


def _hook_from_module(module: ModuleType, path: Path) -> ModelHook:
    exported = getattr(module, "MODEL_HOOK", None)
    if isinstance(exported, ModelHook):
        return exported
    model_id = str(getattr(module, "MODEL_ID", "") or "").strip()
    if not model_id:
        raise ModelHookError(f"model hook must export MODEL_HOOK or MODEL_ID: {path}")
    instructions = getattr(module, "DEVELOPER_INSTRUCTIONS", ())
    if isinstance(instructions, str):
        instructions = (instructions,)
    adjust_messages = getattr(module, "adjust_messages", None)
    if adjust_messages is not None and not callable(adjust_messages):
        raise ModelHookError(f"adjust_messages is not callable: {path}")
    adjust_tools = getattr(module, "adjust_tools", None)
    if adjust_tools is not None and not callable(adjust_tools):
        raise ModelHookError(f"adjust_tools is not callable: {path}")
    if hasattr(module, "adjust_generation_policy"):
        raise ModelHookError(
            f"adjust_generation_policy is forbidden; model hooks may only adjust messages and tools: {path}"
        )
    return ModelHook(
        model_id=model_id,
        developer_instructions=tuple(instructions or ()),
        adjust_messages=adjust_messages,
        adjust_tools=adjust_tools,
    )


def _apply_messages(
    hook: MessageHook | None,
    messages: tuple[LLMMessageIR, ...],
) -> tuple[LLMMessageIR, ...]:
    if hook is None:
        return messages
    result = hook(messages)
    if result is None:
        return messages
    normalized = tuple(result)
    if any(not isinstance(item, LLMMessageIR) for item in normalized):
        raise ModelHookError("adjust_messages must return only LLMMessageIR values")
    return normalized


def _apply_tools(
    hook: ToolHook | None,
    tools: tuple[ToolDefinitionIR, ...],
) -> tuple[ToolDefinitionIR, ...]:
    if hook is None:
        return tools
    result = hook(tools)
    if result is None:
        return tools
    normalized = tuple(result)
    if any(not isinstance(item, ToolDefinitionIR) for item in normalized):
        raise ModelHookError("adjust_tools must return only ToolDefinitionIR values")
    return normalized

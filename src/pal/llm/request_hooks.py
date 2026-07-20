from __future__ import annotations

from typing import Any, Callable, Iterable

from pal.llm.contracts import CanonicalLLMRequest
from pal.llm.llm_adaptor.base import _capabilities, _normalize_key
from pal.llm.models import LLMEndpointModel


BEHAVIOR_ROUTING_HOOK = "behavior_routing"
LEGACY_MAIN_BEHAVIOR_ROUTING_HOOK = "main_behavior_routing"
MAIN_BEHAVIOR_ROUTING_HOOK = BEHAVIOR_ROUTING_HOOK
DEFAULT_LLM_REQUEST_HOOKS = (BEHAVIOR_ROUTING_HOOK,)
MAIN_LLM_REQUEST_HOOKS = DEFAULT_LLM_REQUEST_HOOKS

MessageHook = Callable[[LLMEndpointModel, CanonicalLLMRequest, list[dict[str, Any]]], list[dict[str, Any]]]


def apply_llm_message_hooks(
    endpoint: LLMEndpointModel,
    request: CanonicalLLMRequest,
    messages: list[dict[str, Any]],
    *,
    hooks: Iterable[str] = (),
) -> list[dict[str, Any]]:
    rendered = messages
    for hook_name in _hook_names(hooks):
        hook = _MESSAGE_HOOKS.get(hook_name)
        if hook is not None:
            rendered = hook(endpoint, request, rendered)
    return rendered


def _apply_behavior_routing_hook(
    endpoint: LLMEndpointModel,
    request: CanonicalLLMRequest,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    _ = endpoint
    available_repo_tools = _available_dedicated_repo_tools(request.tools)
    if not _needs_tool_routing_reminder(request, available_repo_tools):
        return messages
    return _append_behavior_routing_reminder(messages)


_MESSAGE_HOOKS: dict[str, MessageHook] = {
    BEHAVIOR_ROUTING_HOOK: _apply_behavior_routing_hook,
    LEGACY_MAIN_BEHAVIOR_ROUTING_HOOK: _apply_behavior_routing_hook,
}
_SHELL_TOOL_NAMES = {"run_shell"}
_DEDICATED_REPO_TOOL_ORDER = (
    "tree",
    "search",
    "read_file",
    "edit_file",
    "write_file",
    "delete_path",
)
_DEDICATED_REPO_TOOL_NAMES = frozenset(_DEDICATED_REPO_TOOL_ORDER)
_BEHAVIOR_ROUTING_REMINDER_MARKER = "behavior-routing-reminder"
_TOOL_ROUTING_REMINDER_MARKER = "tool-routing-reminder"
_LEGACY_ZAI_TOOL_ROUTING_REMINDER_MARKER = "zai-tool-routing-reminder"


def _behavior_routing_reminder() -> str:
    return f"""<system-reminder id="{_BEHAVIOR_ROUTING_REMINDER_MARKER}">
Behavior-routing guidance for this turn:
- Treat this reminder as routing guidance for choosing the right capability. The system prompt still defines the principles and priority order.
- Choose the smallest available capability that matches the user's immediate intent, then reassess after seeing its result.
- Keep work scoped to the current request, active plan, and visible capability policy; do not invent hidden steps or skip required verification.
- Do not guess unavailable capability names. If an intended capability is not present in this request, use the closest available safe path or explain the limitation.
</system-reminder>"""


def is_zai_glm_endpoint(endpoint: LLMEndpointModel) -> bool:
    capabilities = _capabilities(endpoint)
    adapter = _normalize_key(capabilities.get("adapter") or capabilities.get("llm_adapter") or "")
    provider = _normalize_key(getattr(endpoint, "provider", "") or "")
    model_id = _normalize_key(getattr(endpoint, "model_id", "") or "")
    base_url = _normalize_key(getattr(endpoint, "base_url", "") or "")
    return bool(
        adapter in {"glm", "zai", "zai_glm", "zhipu"}
        or provider in {"zai", "zhipu"}
        or model_id.startswith("glm-")
        or "/glm-" in model_id
        or "bigmodel.cn" in base_url
        or "api.z.ai" in base_url
    )


def _available_dedicated_repo_tools(tools: list[dict[str, Any]]) -> set[str]:
    return _tool_names(tools) & _DEDICATED_REPO_TOOL_NAMES


def _needs_tool_routing_reminder(request: CanonicalLLMRequest, available_repo_tools: set[str]) -> bool:
    tool_names = _tool_names(request.tools)
    return bool(tool_names & _SHELL_TOOL_NAMES) and bool(available_repo_tools)


def _append_behavior_routing_reminder(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rendered = [dict(message) for message in list(messages or []) if isinstance(message, dict)]
    reminder = _behavior_routing_reminder()
    if not rendered:
        return [{"role": "user", "content": reminder}]
    if (
        _messages_contain_text(rendered, _BEHAVIOR_ROUTING_REMINDER_MARKER)
        or _messages_contain_text(rendered, _TOOL_ROUTING_REMINDER_MARKER)
        or _messages_contain_text(rendered, _LEGACY_ZAI_TOOL_ROUTING_REMINDER_MARKER)
    ):
        return rendered
    for index in range(len(rendered) - 1, -1, -1):
        if str(rendered[index].get("role") or "").strip().lower() == "user":
            rendered[index]["content"] = _append_text_block(rendered[index].get("content"), reminder)
            return rendered
    rendered.append({"role": "user", "content": reminder})
    return rendered


def _hook_names(raw: Iterable[str]) -> list[str]:
    names = []
    for item in raw:
        normalized = str(item or "").strip().lower()
        if normalized and normalized not in names:
            names.append(normalized)
    return names


def _tool_names(tools: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for tool in list(tools or []):
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "").strip()
        else:
            name = str(tool.get("name") or "").strip()
        if name:
            names.add(name)
    return names


def _append_text_block(content: Any, text: str) -> Any:
    suffix = str(text or "").strip()
    if not suffix:
        return content
    if content is None:
        return suffix
    if isinstance(content, str):
        return f"{content.rstrip()}\n\n{suffix}" if content.strip() else suffix
    if isinstance(content, list):
        blocks = [dict(item) if isinstance(item, dict) else item for item in content]
        blocks.append({"type": "text", "text": suffix})
        return blocks
    return f"{str(content).rstrip()}\n\n{suffix}"


def _messages_contain_text(messages: list[dict[str, Any]], needle: str) -> bool:
    target = str(needle or "").strip()
    if not target:
        return False
    return any(target in _message_text(message.get("content")) for message in messages)


def _message_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(part for part in parts if part)
    return str(content)

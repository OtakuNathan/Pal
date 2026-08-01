from __future__ import annotations

from pathlib import Path
import json
from typing import Any

from pal.llm.contracts import LLMGenerationResult
from pal.llm.ir import ImagePartIR, LLMRequestIR
from pal.llm.serde import message_to_payload


def render_prompt_debug_log(request: LLMRequestIR, *, context: dict[str, Any] | None = None) -> str:
    return "\n".join(
        [
            "=== PAL PROMPT DEBUG ===",
            *_render_context_lines(context),
            "--- request.messages ---",
            json.dumps(
                [message_to_payload(message) for message in request.messages],
                ensure_ascii=False,
                sort_keys=True,
            ),
            "--- request.multimodal ---",
            summarize_multimodal_prompt(request.messages),
            "--- request.tools ---",
            str(request.tools),
            "=== END PAL PROMPT DEBUG ===",
        ]
    )


def render_llm_outcome_debug_log(
    outcome: LLMGenerationResult,
    *,
    provider_payload: str = "",
    context: dict[str, Any] | None = None,
) -> str:
    return "\n".join(
        [
            "=== PAL LLM OUTCOME ===",
            *_render_context_lines(context),
            "--- provider.payload ---",
            provider_payload or "{}",
            f"finish_reason: {outcome.finish_reason}",
            f"response_mode: {outcome.response_mode}",
            f"tool_calls: {outcome.tool_calls}",
            f"reasoning_text (first 500): {str(outcome.reasoning_text or '')[:500]}",
            f"text (first 2000): {str(outcome.text or '')[:2000]}",
            "=== END PAL LLM OUTCOME ===",
        ]
    )


def render_reply_debug_log(text: object, *, context: dict[str, Any] | None = None) -> str:
    return "\n".join(
        [
            "=== PAL REPLY ===",
            *_render_context_lines(context),
            str(text or ""),
            "=== END PAL REPLY ===",
        ]
    )


def append_prompt_debug_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text.rstrip() + "\n")


def summarize_multimodal_prompt(messages: tuple[Any, ...]) -> str:
    items: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        for part in message.parts:
            if isinstance(part, ImagePartIR):
                url = part.source
                items.append(
                    {
                        "message_index": index,
                        "type": "image",
                        "url_prefix": url[:32],
                        "url_length": len(url),
                        "media_type": part.media_type,
                        "bytes": "omitted",
                    }
                )
    return str(items)


def summarize_last_provider_payload(llm_runtime: Any) -> str:
    invoker = getattr(llm_runtime, "endpoint_invoker", None)
    summary = getattr(invoker, "last_payload_summary", None)
    return str(summary or {})


def _render_context_lines(context: dict[str, Any] | None) -> list[str]:
    data = {str(key): value for key, value in dict(context or {}).items() if str(value or "").strip()}
    if not data:
        return []
    return ["--- context ---", str(data)]

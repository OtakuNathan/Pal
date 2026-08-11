from __future__ import annotations

import re
from collections.abc import Iterable, Iterator

from pal.llm.ir import LLMResponseDeltaKind, LLMResponseUpdate
from pal.llm.response_hooks import (
    ProviderResponseHookContext,
    ProviderResponseHookError,
)


_INTERNAL_PROJECTION = re.compile(
    r"<\s*/?\s*closed_tool_interaction\b|"
    r"\[old tool (?:interaction|result) cleared\]",
    flags=re.IGNORECASE,
)
_TEXTUAL_TOOL_PROTOCOL = re.compile(
    r"(?im)^\s*<\s*/?\s*(?:tool_calls?|function_results?)\b|"
    r"<\s*/?\s*[|｜]{1,2}\s*DSML\s*[|｜]{1,2}\s*"
    r"(?:tool_calls|invoke|parameter)\b",
)
_SCAN_TAIL_LIMIT = 256


def normalize_zhipu_updates(
    context: ProviderResponseHookContext,
    updates: Iterable[LLMResponseUpdate],
) -> Iterator[LLMResponseUpdate]:
    """Reject leaked textual tool protocols so endpoint retry can recover.

    Zhipu's native structured tool items are already decoded by the selected
    wire-shape codec.  Reconstructing a second protocol from assistant text is
    ambiguous and unsafe; treating the response as malformed is the honest
    fallback.
    """

    tails = {
        LLMResponseDeltaKind.TEXT: "",
        LLMResponseDeltaKind.REASONING: "",
    }
    saw_text_delta = False
    for update in updates:
        projected = ""
        if update.delta_kind in tails and update.text_delta:
            saw_text_delta = True
            projected = tails[update.delta_kind] + update.text_delta
            tails[update.delta_kind] = projected[-_SCAN_TAIL_LIMIT:]
        elif (
            update.delta_kind == LLMResponseDeltaKind.STATE
            and not saw_text_delta
        ):
            # Single-shot/custom codecs may expose only one terminal snapshot.
            # Do this once, never once per streamed delta.
            projected = "\n".join(
                value
                for value in (
                    update.response.reasoning_text,
                    update.response.text,
                )
                if value
            )
        if _INTERNAL_PROJECTION.search(projected) or (
            context.request.tools and _TEXTUAL_TOOL_PROTOCOL.search(projected)
        ):
            raise ProviderResponseHookError(
                "Zhipu emitted a textual or internal tool protocol instead of structured tool items"
            )
        yield update


__all__ = ["normalize_zhipu_updates"]

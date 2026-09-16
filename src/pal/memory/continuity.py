"""One compact seed, owned by L1 and projected as historical user reference."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from pal.llm.ir import LLMMessageIR, MessageRole, PromptRegionIR, TextPartIR
from pal.memory.contracts import L1MessageKind, L2Entry
from pal.shared.json_values import thaw_json

ANCHOR_KEY = "continuity_anchor"
SUMMARY_CONTEXT_KEY = "reference:memory.prompt.default:memory_current_summary:Conversation summary"


def is_summary_projection(message: LLMMessageIR) -> bool:
    return message.semantic_kind == L1MessageKind.RUNTIME_CONTEXT_SUMMARY or bool(
        message.semantic_kind == "pal_prompt_context"
        and message.metadata.get("pal_authored")
        and message.metadata.get("context_key") == SUMMARY_CONTEXT_KEY
    )


@dataclass(frozen=True)
class Continuity:
    source_id: str
    entry: L2Entry
    part: TextPartIR
    anchor: str = ""

    @classmethod
    def from_message(cls, message: LLMMessageIR, metadata: Mapping[str, Any]) -> Continuity:
        payload = thaw_json(message.metadata)
        summary = payload.get("summary", {})
        summary = summary if isinstance(summary, dict) else {}
        text = message.text.strip()
        entry = L2Entry(
            entry_id="memory_summary_current", kind="summary", scope="system",
            title="Conversation Summary", source_kind="l1_compaction", candidate_state="stable",
            summary=str(summary.get("summary") or text).strip(), rendered=text,
            search_text=str(summary.get("search_text") or summary.get("summary") or text).strip(),
            payload=payload,
        )
        if not text.startswith(("<conversation_summary", "<compact_context")):
            text = f"<conversation_summary>\n{text}\n</conversation_summary>"
        return cls(message.message_id, entry, TextPartIR(text + "\n\n"),
                   str(metadata.get(ANCHOR_KEY) or ""))

    @property
    def standalone_id(self) -> str:
        return f"continuity:{self.source_id}"

    def project(self, messages: list[LLMMessageIR]) -> list[LLMMessageIR]:
        """Attach the seed to already-selected request messages, not raw L1."""
        messages = list(messages)
        for index, message in enumerate(messages):
            if message.message_id == self.anchor:
                if message.metadata.get("continuity_id") != self.source_id:
                    messages[index] = replace(message, parts=(self.part, *message.parts),
                                              metadata={**message.metadata, "continuity_id": self.source_id})
                return messages
        # A request without the anchored input still needs its compact seed.
        # Keep tool call/result adjacency intact by inserting before all history.
        index = next((i for i, m in enumerate(messages)
                      if m.role not in {MessageRole.SYSTEM, MessageRole.DEVELOPER}), len(messages))
        messages.insert(index, LLMMessageIR(
            role=MessageRole.USER, parts=(self.part,), message_id=self.standalone_id,
            semantic_kind="conversation_continuity", prompt_region=PromptRegionIR.SETTLED_HISTORY,
            metadata={"continuity_id": self.source_id}))
        return messages

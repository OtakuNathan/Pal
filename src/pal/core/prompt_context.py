"""Pure planning of immutable context records and independently tracked coverage."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from html import escape
import json
from typing import Any, Mapping, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from pal.memory.turn_ir import L1TurnIR

from pal.llm.ir import LLMMessageIR, MessageRole, PromptRegionIR, TextPartIR
from pal.llm.serde import message_from_payload, message_to_payload
from pal.shared.json_values import thaw_json

STATE_KEY = "prompt_context_state"
CONTEXT_KIND = "pal_prompt_context"


def prepare_context(
    turn: "L1TurnIR", stable_messages: Sequence[LLMMessageIR],
    candidates: Sequence[Mapping[str, Any]], *, boundary: str = "",
) -> tuple[tuple[LLMMessageIR, ...], tuple[LLMMessageIR, ...], dict[str, Any]]:
    """Return frozen instructions, records to append, and their atomic coverage state.

    Source revisions advance on semantic changes, never on rendering or recovery.
    Coverage is tested against actual L1 records, not only a remembered revision.
    """
    state = thaw_json(turn.metadata.get(STATE_KEY, {}))
    if not state:
        state = {"version": 1, "stable": [message_to_payload(m) for m in stable_messages],
                 "sources": {}, "events": {}}
    if state.get("version") != 1:
        raise ValueError("unsupported prompt context state version")
    rebuild = state.get("boundary", boundary) != boundary
    state["boundary"] = boundary
    if rebuild:
        state["stable"] = [message_to_payload(m) for m in stable_messages]
    frozen = tuple(message_from_payload(m) for m in state["stable"])
    visible = {m.message_id for m in projected_context(turn)}
    additions = []
    candidates = thaw_json(candidates)
    states = [item for item in candidates if item.get("kind", "state") != "event"]
    events = [item for item in candidates if item.get("kind") == "event"]
    current = {item["key"]: item for item in states}
    if len(current) != len(states):
        raise ValueError("duplicate prompt context state key")
    sources = state["sources"]
    if any(item["key"] in sources or item["key"] in current for item in events):
        raise ValueError("context source cannot change from state to event within a turn")
    # Absence from a complete source snapshot explicitly withdraws a previous state.
    for key, old in tuple(sources.items()):
        if key not in current and not old.get("withdrawn"):
            current[key] = {"key": key, "role": old["role"], "content": "", "withdrawn": True,
                            "kind": "state", "instruction": old.get("instruction", False),
                            "coverage_kind": old.get("coverage_kind", "")}
    for key, item in [*current.items(), *((item["key"], item) for item in events)]:
        kind = item.get("kind", "state")
        if kind not in {"state", "event"}:
            raise ValueError("unknown prompt context kind")
        role = item.get("role", "developer")
        parts = (item.get("ir_message") or {}).get("parts") or item.get("parts") or [{"type": "text", "text": item.get("content", "")}]
        if isinstance(parts, str):
            parts = [{"type": "text", "text": parts}]
        semantic = {"parts": parts, "role": role, "withdrawn": bool(item.get("withdrawn")),
                    "instruction": bool(item.get("instruction"))}
        digest = sha256(json.dumps(semantic, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        if kind == "event":
            event_id = json.dumps([key, str(item["event_id"])])
            if event_id in state["events"]:
                if state["events"][event_id] != digest:
                    raise ValueError("context event identity reused with different content")
                continue
            state["events"][event_id] = digest
            revision = 1
            previous = None
        else:
            previous = sources.get(key)
            changed = previous is None or previous["digest"] != digest
            revision = item.get("source_revision")
            if revision is not None:
                if type(revision) is not int or revision < 1:
                    raise ValueError("source revision must be a positive integer")
                if previous and (revision < previous["revision"] or (changed and revision == previous["revision"])):
                    raise ValueError("source revision does not advance changed content")
                changed = changed or previous is None or revision != previous["revision"]
            else:
                revision = (previous["revision"] if previous else 0) + int(changed)
            # Stable instruction blocks are covered by the frozen prefix initially.
            if item.get("instruction") and (rebuild or (previous is None and not state.get("initialized"))):
                sources[key] = {"digest": digest, "revision": revision, "role": role,
                                "instruction": True, "prefix": True, "withdrawn": semantic["withdrawn"]}
                continue
            if not changed and (previous.get("prefix") or previous.get("message_id") in visible):
                continue
        if kind == "state" and item.get("coverage_kind") == "checklist":
            covered = _checklist_coverage(turn.messages, item)
            if covered:
                sources[key] = {"digest": digest, "revision": revision, "message_id": covered,
                                "role": role, "withdrawn": semantic["withdrawn"], "coverage_kind": "checklist"}
                continue
        sequence = int(state.get("delivery_sequence", 0)) + 1
        state["delivery_sequence"] = sequence
        message_id = f"prompt-context:{turn.turn_id}:{sequence}"
        withdrawn = semantic["withdrawn"]
        action = "withdraw" if withdrawn else "occurred" if kind == "event" else "restore" if previous and not changed else "replace"
        attributes = (f'kind="{kind}" key="{escape(key, quote=True)}" revision="{revision}" '
                      f'scope="turn" turn_id="{escape(turn.turn_id, quote=True)}" '
                      f'action="{action}"')
        if kind == "event":
            attributes += f' event_id="{escape(str(item["event_id"]), quote=True)}"'
        if previous:
            attributes += f' replaces="{previous["revision"]}"'
        header = f"<pal_context {attributes}>\n"
        if item.get("instruction"):
            header += "<pal_defaults>Unless the current user request specifies otherwise:\n"
        footer = ("\n</pal_defaults>" if item.get("instruction") else "") + "\n</pal_context>"
        # Keep artifact/reference parts as user data. Control records only carry text.
        from pal.llm.conversions import request_ir_from_prompt
        body = [{"type": "text", "text": header}, *parts, {"type": "text", "text": footer}]
        if item.get("ir_message"):
            original = message_from_payload(item["ir_message"])
            message = replace(original, parts=(TextPartIR(header), *original.parts, TextPartIR(footer)))
        else:
            message = request_ir_from_prompt(messages=[{"role": role, "content": body}], max_output_tokens=1).messages[0]
        message = replace(message, message_id=message_id, semantic_kind=CONTEXT_KIND,
                          prompt_region=PromptRegionIR.ACTIVE_HISTORY,
                          metadata={"pal_authored": True, "scope_turn_id": turn.turn_id,
                                    "context_key": key, "context_kind": kind, "source_revision": revision,
                                    "withdrawn": withdrawn})
        additions.append(message)
        if kind == "state":
            sources[key] = {"digest": digest, "revision": revision, "message_id": message_id,
                            "role": role, "withdrawn": withdrawn, "instruction": bool(item.get("instruction")),
                            "coverage_kind": item.get("coverage_kind", "")}
    if rebuild:
        retained = {source.get("message_id") for source in sources.values()
                    if not source.get("withdrawn") and not source.get("instruction")}
        retained.update(m.message_id for m in additions if m.metadata["context_kind"] == "event")
        state["excluded"] = sorted(set(state.get("excluded", ())) | {
            m.message_id for m in (*turn.messages, *additions)
            if m.semantic_kind == CONTEXT_KIND and m.message_id not in retained})
    state["initialized"] = True
    return frozen, tuple(additions), state


def applicable_context(messages: Sequence[LLMMessageIR], *, active_turn_id: str) -> list[LLMMessageIR]:
    """Old control records never become active instructions in another turn."""
    return [m for m in messages if not (
        m.semantic_kind == CONTEXT_KIND and m.metadata.get("pal_authored")
        and m.role == MessageRole.DEVELOPER
        and m.metadata.get("scope_turn_id") != active_turn_id
    )]


def projected_context(turn: "L1TurnIR") -> list[LLMMessageIR]:
    excluded = set(turn.metadata.get(STATE_KEY, {}).get("excluded", ()))
    return [m for m in turn.messages if m.message_id not in excluded]


def completed_tool_contexts(continuation: Any, turn: "L1TurnIR") -> tuple[LLMMessageIR, ...]:
    """Recover sidecars from committed results when a batch is interrupted."""
    from pal.llm.ir import ArtifactRefPartIR
    from pal.shared.tool_protocol import ToolResultIR
    committed = {part.call_id for message in turn.messages for part in message.parts if isinstance(part, ToolResultIR)}
    contexts = []
    for call, result in zip(continuation.pending_tool_call_batch, continuation.pending_tool_results):
        if call.call_id not in committed:
            continue
        for index, context in enumerate(result.context_messages):
            contexts.append(LLMMessageIR(
                role=MessageRole.USER,
                parts=(TextPartIR(context.content), *(ArtifactRefPartIR(artifact_id=a) for a in context.artifact_ids)),
                message_id=f"tool-context:{call.call_id}:{index}", semantic_kind=context.semantic_kind,
                metadata={**dict(context.metadata), "source_tool_call_id": call.call_id, "source_tool_name": call.name}))
    return tuple(contexts)


def _checklist_coverage(messages: Sequence[LLMMessageIR], item: Mapping[str, Any]) -> str | None:
    """An exact current checklist echo already delivers this state to the model."""
    from html import unescape
    from pal.shared.tool_protocol import ToolResultIR
    for message in reversed(messages):
        for part in message.parts:
            if not isinstance(part, ToolResultIR):
                continue
            try:
                payload, _ = json.JSONDecoder().raw_decode(part.content[part.content.index("{"):])
            except (ValueError, TypeError):
                continue
            echo = payload.get("echo") if isinstance(payload, dict) else None
            if not isinstance(echo, dict) or echo.get("tag") != "checklist":
                continue
            if item.get("withdrawn"):
                return message.message_id if not echo.get("payload", {}).get("active", True) else None
            markdown = str(echo.get("markdown", ""))
            return message.message_id if markdown and markdown in unescape(item.get("content", "")) else None
    return None

"""Pure planning of immutable context records and independently tracked coverage."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from html import escape
import json
from typing import Any, Mapping, Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from pal.memory.turn_ir import L1TurnIR

from pal.llm.ir import LLMMessageIR, MessageRole, PromptRegionIR, TextPartIR, InstructionScopeIR
from pal.llm.serde import message_from_payload, message_to_payload
from pal.shared.json_values import thaw_json

STATE_KEY = "prompt_context_state"
CONTEXT_KIND = "pal_prompt_context"
_CHECKLIST_TOOLS = frozenset({
    "upsert_checklist", "op_checklist_upsert", "complete_checklist_step",
    "op_checklist_check", "read_checklist", "op_checklist_show",
    "close_checklist", "op_checklist_clear",
})

def prompt_date_pin(memory: Any) -> dict[str, Any] | None:
    """Find the latest durable date pin in the current compact generation."""
    root = memory.history_root
    epoch = (root.incarnation, root.left_generation)
    for turn in reversed(memory.history.turns):
        pin = turn.metadata.get(STATE_KEY, {}).get("date_pin", {})
        if tuple(pin.get("epoch", ())) == epoch and pin.get("date"):
            return thaw_json(pin)
    return None


def select_prompt_date_pin(memory: Any, turn: "L1TurnIR", today: str, *,
                           cold: bool = False, borrowed_date: str | None = None) -> dict[str, Any]:
    root = memory.history_root
    epoch = [root.incarnation, root.left_generation]
    state = turn.metadata.get(STATE_KEY, {})
    own = state.get("date_pin", {})
    if tuple(own.get("epoch", ())) == tuple(epoch) and "date" in own:
        return thaw_json(own)
    previous = prompt_date_pin(memory)
    date = borrowed_date or (previous["date"] if previous and not cold else today)
    return {"epoch": epoch, "date": date}


def _remaining(plan: Any) -> tuple[str, ...] | None:
    if not isinstance(plan, (list, tuple)):
        return None
    result = []
    for item in plan:
        if not isinstance(item, Mapping) or not isinstance(item.get("step"), str):
            return None
        if item.get("status", "pending") != "completed":
            result.append(item["step"].strip())
    return tuple(result)


def _checklist_view(messages: Sequence[LLMMessageIR]) -> tuple[tuple[str, ...] | None, bool | None]:
    """Reconstruct only state actually delivered in the visible conversation."""
    from pal.shared.tool_protocol import ToolCallIR, ToolResultIR
    calls: dict[str, ToolCallIR] = {}
    remaining: tuple[str, ...] | None = None
    active: bool | None = None
    for message in messages:
        if message.metadata.get("history_fact") == "checklist":
            active = bool(message.metadata.get("checklist_active"))
            if "remaining" in message.metadata:
                remaining = tuple(message.metadata["remaining"])
            elif not active:
                remaining = ()
            elif remaining is not None and len(remaining) != message.metadata.get("remaining_count"):
                remaining = None
        for part in message.parts:
            if isinstance(part, ToolCallIR):
                if part.name in _CHECKLIST_TOOLS:
                    calls[part.call_id] = part
                continue
            if not isinstance(part, ToolResultIR) or not part.ok:
                continue
            name = part.name
            if name not in _CHECKLIST_TOOLS:
                continue
            try:
                data, _ = json.JSONDecoder().raw_decode(part.content[part.content.index("{"):])
            except (ValueError, TypeError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            call = calls.get(part.call_id)
            if name in {"close_checklist", "op_checklist_clear"}:
                if "cleared" in data:
                    remaining, active = (), False
                else:
                    remaining, active = None, None
            elif name in {"read_checklist", "op_checklist_show"}:
                if data.get("active") is False or part.content == "No active checklist.":
                    remaining, active = (), False
                else:
                    remaining = _remaining(data.get("plan"))
                    active = bool(remaining) if remaining is not None else None
            elif name in {"upsert_checklist", "op_checklist_upsert"}:
                remaining = _remaining(call.arguments.get("plan")) if call is not None and "active" in data else None
                active = bool(data["active"]) if "active" in data else None
                if active is False:
                    remaining = ()
                elif remaining is not None and (type(data.get("total")) is not int
                        or type(data.get("done")) is not int
                        or len(remaining) != data["total"] - data["done"]):
                    remaining = None
            else:
                if data.get("cleared") is True:
                    remaining, active = (), False
                elif ("step" in data and type(data.get("done")) is int
                      and type(data.get("total")) is int):
                    active = True
                    if remaining is not None:
                        remaining = tuple(step for step in remaining if step != data["step"])
                        if len(remaining) != data["total"] - data["done"]:
                            remaining = None
                else:
                    remaining, active = None, None
    return remaining, active


def _append_history_facts(turn: "L1TurnIR", state: dict[str, Any],
                          messages: Sequence[LLMMessageIR], additions: list[LLMMessageIR],
                          *, date_pin: Mapping[str, Any] | None, today: str,
                          checklist: Mapping[str, Any] | None) -> None:
    def fact(kind: str, text: str, **metadata: Any) -> None:
        sequence = int(state.get("delivery_sequence", 0)) + 1
        state["delivery_sequence"] = sequence
        additions.append(LLMMessageIR(
            role=MessageRole.USER, parts=(TextPartIR("Pal runtime: " + text),),
            message_id=f"prompt-context:{turn.turn_id}:{sequence}", semantic_kind=CONTEXT_KIND,
            prompt_region=PromptRegionIR.ACTIVE_HISTORY,
            metadata={"pal_authored": True, "history_fact": kind, "scope_turn_id": turn.turn_id,
                      "context_kind": "fact", **metadata},
        ))

    if date_pin is not None:
        state["date_pin"] = thaw_json(date_pin)
        last_date = next((m.metadata.get("utc_date") for m in reversed(messages)
                          if m.metadata.get("history_fact") == "date"), None)
        if today and today != date_pin["date"] and last_date != today:
            fact("date", f"UTC date update: {today}", utc_date=today)
    if checklist is None:
        return
    remaining = tuple(checklist.get("remaining", ()))
    known, was_active = _checklist_view(messages)
    active = bool(checklist.get("active"))
    if not active:
        if was_active:
            fact("checklist", "Checklist closed.", checklist_active=False, remaining=[])
        return
    reminder_seen = any(m.metadata.get("history_fact") == "checklist"
                        and m.metadata.get("scope_turn_id") == turn.turn_id
                        for m in messages)
    if known != remaining:
        fact("checklist", f"Checklist: {len(remaining)} unfinished steps.\n" +
             "\n".join(f"- {step}" for step in remaining),
             checklist_active=True, remaining=list(remaining), remaining_count=len(remaining))
    elif not reminder_seen and not state.get("initialized"):
        fact("checklist", f"Checklist: {len(remaining)} unfinished steps.",
             checklist_active=True, remaining_count=len(remaining))


def prepare_context(
    turn: "L1TurnIR", stable_messages: Sequence[LLMMessageIR],
    candidates: Sequence[Mapping[str, Any]], *, boundary: str = "",
    date_pin: Mapping[str, Any] | None = None, today: str = "",
    checklist: Mapping[str, Any] | None = None,
    settled_history: Sequence[LLMMessageIR] = (),
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
    previous_pin = state.get("date_pin")
    rebuild = (state.get("boundary", boundary) != boundary
               or (date_pin is not None and previous_pin is not None
                   and tuple(previous_pin["epoch"]) != tuple(date_pin["epoch"])))
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
    # Compact continuity is now projected once, outside turn-scoped updates.
    from pal.memory.continuity import SUMMARY_CONTEXT_KEY
    sources.pop(SUMMARY_CONTEXT_KEY, None)
    if any(item["key"] in sources or item["key"] in current for item in events):
        raise ValueError("context source cannot change from state to event within a turn")
    # Absence from a complete source snapshot explicitly withdraws a previous state.
    for key, old in tuple(sources.items()):
        if key not in current and not old.get("withdrawn"):
            current[key] = {"key": key, "role": old["role"], "content": "", "withdrawn": True,
                            "kind": "state", "instruction": old.get("instruction", False),
                            "coverage_kind": old.get("coverage_kind", ""), "title": old.get("title", "")}
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
            title = str((previous or {}).get("title") or item.get("title") or "").strip()
            if not (previous or {}).get("title"):
                base = title or "Context"
                used = {source.get("title") for source in sources.values()}
                title = base
                suffix = 2
                while title in used:
                    title = f"{base} ({suffix})"
                    suffix += 1
                if previous is not None:
                    previous["title"] = title
            # Stable instruction blocks are covered by the frozen prefix initially.
            if item.get("instruction") and (rebuild or (previous is None and not state.get("initialized"))):
                sources[key] = {"digest": digest, "revision": revision, "role": role,
                                "instruction": True, "prefix": True, "withdrawn": semantic["withdrawn"], "title": title}
                continue
            if not changed and (previous.get("prefix") or previous.get("message_id") in visible):
                continue
        if kind == "state" and item.get("coverage_kind") == "checklist":
            covered = _checklist_coverage(turn.messages, item)
            if covered:
                sources[key] = {"digest": digest, "revision": revision, "message_id": covered,
                                "role": role, "withdrawn": semantic["withdrawn"], "coverage_kind": "checklist", "title": title}
                continue
        sequence = int(state.get("delivery_sequence", 0)) + 1
        state["delivery_sequence"] = sequence
        message_id = f"prompt-context:{turn.turn_id}:{sequence}"
        withdrawn = semantic["withdrawn"]
        action = "withdraw" if withdrawn else "occurred" if kind == "event" else "restore" if previous and not changed else "replace"
        if kind == "event":
            title = str(item.get("title") or "Context event").strip()
        header = f'<pal_context name="{escape(title, quote=True)}">\n'
        if withdrawn:
            header += "The earlier context under this heading no longer applies.\n"
        elif previous:
            header += "Use this context in place of earlier content under this heading.\n"
        if item.get("instruction"):
            header += "<pal_defaults>\n"
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
                                    "withdrawn": withdrawn, "context_title": title, "context_action": action,
                                    **({"event_id": str(item["event_id"])} if kind == "event" else {}),
                                    **({"replaces_revision": previous["revision"]} if previous else {})})
        additions.append(message)
        if kind == "state":
            sources[key] = {"digest": digest, "revision": revision, "message_id": message_id,
                            "role": role, "withdrawn": withdrawn, "instruction": bool(item.get("instruction")),
                            "coverage_kind": item.get("coverage_kind", ""), "title": title}
    if rebuild:
        retained = {source.get("message_id") for source in sources.values()
                    if not source.get("withdrawn") and not source.get("instruction")}
        retained.update(m.message_id for m in additions if m.metadata["context_kind"] == "event")
        state["excluded"] = sorted(set(state.get("excluded", ())) | {
            m.message_id for m in (*turn.messages, *additions)
            if m.semantic_kind == CONTEXT_KIND and m.message_id not in retained
            and not m.metadata.get("history_fact")
            and m.instruction_scope != InstructionScopeIR.NEXT_USER_MESSAGE})
    if date_pin is not None or checklist is not None:
        if (date_pin is not None and state.get("initialized") and not rebuild
                and "date_pin" not in state):
            # A live legacy snapshot never acquires an invented preamble.
            date_pin = {**date_pin, "date": ""}
        _append_history_facts(turn, state, (*settled_history, *(
            m for m in projected_context(turn) if m.message_id not in state.get("excluded", ())
        )), additions, date_pin=date_pin, today=today, checklist=checklist)
    state["initialized"] = True
    return frozen, tuple(additions), state


def applicable_context(messages: Sequence[LLMMessageIR], *, active_turn_id: str,
                       preserve_turn_scoped: bool = False) -> list[LLMMessageIR]:
    """Old control records never become active instructions in another turn."""
    return [m for m in messages if not (
        m.semantic_kind == CONTEXT_KIND and m.metadata.get("pal_authored")
        and m.role == MessageRole.DEVELOPER
        and m.metadata.get("scope_turn_id") != active_turn_id
        and not (preserve_turn_scoped and m.instruction_scope == InstructionScopeIR.NEXT_USER_MESSAGE)
    )]


def projected_context(turn: "L1TurnIR") -> list[LLMMessageIR]:
    from pal.memory.context_view import projected_messages
    return list(projected_messages(turn))


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

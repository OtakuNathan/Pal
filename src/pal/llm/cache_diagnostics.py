"""Bounded cache diagnostics describe visible wire bytes, not provider tokens."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from pal.llm.ir import PromptRegionIR
from pal.shared.json_values import thaw_json


def fingerprint(value: Any) -> tuple[str, int]:
    data = json.dumps(thaw_json(value), ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest(), len(data)


def at_path(payload: Any, path: tuple) -> Any:
    for part in path:
        payload = payload[part]
    return payload


def describe_request(request, raw_encoded, encoded) -> dict[str, Any]:
    """Describe the visible wire in span-independent, non-overlapping units.

    R5 (review 95373ef): the unit set comes from the payload STRUCTURE
    alone — top-level system, every conversation item, and the blocks of
    list content — so identical bytes always produce identical units no
    matter which spans travel; spans only ATTRIBUTE units to the request
    messages that claim them.  Unclaimed units inherit the unspanned
    messages' region when it is uniform (the frozen-prefix case); mixed or
    unresolvable attribution becomes ``unknown`` and is never ignorable, so
    drift can not hide behind a last-writer-wins label.  An item's bytes are
    never described twice (no whole-item cache_targets re-enumeration).
    """

    payload = raw_encoded.payload
    spans = {s.message_id: s for s in raw_encoded.message_spans}

    def component_for(message) -> str:
        return ("dynamic" if message.prompt_region == PromptRegionIR.ACTIVE_DYNAMIC else
                "stable" if message.prompt_region == PromptRegionIR.STABLE_SYSTEM else "history")

    container_key = next((key for key in ("input", "messages")
                          if isinstance(payload.get(key), list)), None)
    # -- attribution maps: spans are a path map, never a presence gate -----
    item_claims: dict[int, set[str]] = {}
    block_claims: dict[tuple[int, int], set[str]] = {}
    system_claims: dict[Any, set[str]] = {}
    unspanned: list[str] = []
    for message in request.messages:
        span = spans.get(message.message_id)
        if span is None:
            unspanned.append(component_for(message))
            continue
        component = component_for(message)
        for path in (span.wire_item_paths or span.cache_targets):
            if not path:
                continue
            if (container_key is not None and len(path) >= 2
                    and path[0] == container_key and isinstance(path[1], int)):
                if len(path) >= 4 and path[2] == "content" and isinstance(path[3], int):
                    block_claims.setdefault(
                        (int(path[1]), int(path[3])), set()).add(component)
                else:
                    item_claims.setdefault(int(path[1]), set()).add(component)
            elif path[0] == "system":
                key = path[1] if len(path) >= 2 and isinstance(path[1], int) else None
                system_claims.setdefault(key, set()).add(component)

    def fallback_component() -> str:
        unique = set(unspanned)
        return unspanned[0] if len(unique) == 1 else "history"

    def attribution(*claim_sets: set[str]) -> str:
        claims: set[str] = set()
        for claim_set in claim_sets:
            claims |= claim_set
        if len(claims) == 1:
            return next(iter(claims))
        if claims:
            return "unknown"  # mixed ownership proves neither region
        return fallback_component()

    units: list[tuple[str, Any]] = []

    # 1) Top-level system is request content; each element is one unit.
    system_value = payload.get("system")
    if isinstance(system_value, list):
        for index, part in enumerate(system_value):
            units.append((
                attribution(system_claims.get(index, set()),
                            system_claims.get(None, set())),
                part,
            ))
    elif isinstance(system_value, str) and system_value:
        units.append((attribution(system_claims.get(None, set())), system_value))

    # 2) Conversation items: list content splits into an item envelope plus
    #    one unit per block; anything else stays one whole-item unit.
    if container_key is not None:
        for index, item in enumerate(payload[container_key]):
            item_claim = item_claims.get(index, set())
            content = item.get("content") if isinstance(item, dict) else None
            if isinstance(content, list):
                envelope = {key: value for key, value in item.items() if key != "content"}
                units.append((attribution(item_claim), envelope))
                for block_index, block in enumerate(content):
                    units.append((
                        attribution(
                            block_claims.get((index, block_index), set()), item_claim),
                        block,
                    ))
            else:
                units.append((attribution(item_claim), item))

    groups: dict[str, list] = {"stable": [], "history": [], "dynamic": [], "unknown": [],
                               "tools": [payload.get("tools", ())]}
    items: list[tuple[str, str, int]] = []
    for region, value in units:
        digest, size = fingerprint(value)
        items.append((region, digest, size))
        groups[region].append(value)
    components = {key: dict(zip(("hash", "bytes"), fingerprint(value))) for key, value in groups.items()}
    markers = []
    applied = set(encoded.applied_cache_breakpoint_message_ids)
    for span in encoded.message_spans:
        if span.message_id not in applied:
            continue
        paths = dict.fromkeys((*span.cache_targets, *((span.continuity_target,) if span.continuity_target else ())))
        for path in paths:
            target = at_path(encoded.payload, path)
            if not isinstance(target, dict) and not hasattr(target, "get"):
                continue
            marker = "prompt_cache_breakpoint" if target.get("prompt_cache_breakpoint") else "cache_control"
            if not target.get(marker):
                continue
            parent = at_path(encoded.payload, path[:2])
            markers.append({"path": [*path, marker], "message_id": span.message_id,
                            "parent_item_type": parent.get("type", parent.get("role", "unknown"))})
    parameters = {k: v for k, v in payload.items() if k not in {"input", "messages", "system", "tools", "max_tokens", "max_output_tokens", "temperature"}}
    return {"components": components, "_items": items, "parameters_hash": fingerprint(parameters)[0],
            "wire_hash": fingerprint({"payload": encoded.payload, "extra_body": encoded.extra_body})[0],
            "applied_marker_paths": markers}


def compare_requests(previous: dict | None, current: dict) -> dict[str, Any]:
    """Compare two wire descriptions by BYTES, not by coverage labels.

    R5 (review 95373ef): only digests may decide whether content drifted —
    an attribution change (a span appeared or vanished) must never read as
    a byte change, or identical wire would keep alarming.  Attribution
    still decides which units are ignorable (a ``dynamic`` unit that
    changed) and which are conservatively part of the prefix (everything
    else, including ``unknown``).
    """

    if previous is None:
        return {"change_reason": "first_request", "prefix_preserved": False,
                "first_different_item": None, "first_different_component": None}
    before, after = previous["_items"], current["_items"]

    def identity(entry: tuple) -> tuple[str, int]:
        return (entry[1], entry[2])

    first = next((i for i, pair in enumerate(zip(before, after))
                  if identity(pair[0]) != identity(pair[1])), None)
    if first is None and len(before) != len(after):
        first = min(len(before), len(after))
    component = (after[first][0] if first is not None and first < len(after) else
                 before[first][0] if first is not None and first < len(before) else None)
    # Dynamic suffix displacement is expected as completed tool history grows.
    old_prefix = [identity(i) for i in before if i[0] != "dynamic"]
    new_prefix = [identity(i) for i in after if i[0] != "dynamic"]
    preserved = old_prefix == new_prefix[:len(old_prefix)]
    tools_equal = previous["components"].get("tools") == current["components"].get("tools")
    parameters_equal = previous["parameters_hash"] == current["parameters_hash"]
    dynamic_equal = previous["components"].get("dynamic") == current["components"].get("dynamic")
    reason = ("tools_changed" if not tools_equal else "parameters_changed" if not parameters_equal else
              "prefix_changed" if not preserved else "dynamic_changed" if not dynamic_equal else
              "history_appended" if len(new_prefix) > len(old_prefix) else "unchanged")
    return {"change_reason": reason, "first_different_item": first,
            "first_different_component": "tools" if not tools_equal else component,
            "prefix_preserved": preserved and tools_equal and parameters_equal,
            "dynamic_unchanged": dynamic_equal}

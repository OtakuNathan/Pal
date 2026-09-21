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
    payload = raw_encoded.payload
    spans = {s.message_id: s for s in raw_encoded.message_spans}
    groups: dict[str, list] = {"stable": [], "history": [], "dynamic": [], "tools": [payload.get("tools", ())]}
    items: list[tuple[str, str, int]] = []

    def describe(component: str, value: Any) -> None:
        digest, size = fingerprint(value)
        items.append((component, digest, size))
        groups[component].append(value)

    def component_for(message) -> str:
        return ("dynamic" if message.prompt_region == PromptRegionIR.ACTIVE_DYNAMIC else
                "stable" if message.prompt_region == PromptRegionIR.STABLE_SYSTEM else "history")

    # Wire-first enumeration (I11): spans are a path map, not a presence
    # gate.  A derived projection re-encodes only the seam and the tail
    # (PLAN §6.2), so frozen-prefix messages keep their wire bytes while
    # their spans do not travel with the assembled request.  Conversation
    # container items that no span claims are still described, from the
    # wire itself, so consecutive rounds compare bytes rather than span
    # coverage.  Unclaimed items inherit the unspanned messages' region
    # when it is uniform (the frozen-prefix case); mixed or envelope-only
    # leftovers default to "history".
    container_key = next((key for key in ("input", "messages")
                          if isinstance(payload.get(key), list)), None)
    claimed: dict[int, str] = {}
    extras: list[tuple[str, tuple]] = []
    unspanned: list[str] = []
    for message in request.messages:
        component = component_for(message)
        span = spans.get(message.message_id)
        if span is None:
            unspanned.append(component)
            continue
        for path in span.wire_item_paths or span.cache_targets:
            if (container_key is not None and len(path) >= 2
                    and path[0] == container_key and isinstance(path[1], int)):
                claimed[path[1]] = component
                if len(path) > 2:
                    extras.append((component, path))
            else:
                extras.append((component, path))
    if container_key is not None:
        fallback = unspanned[0] if len(set(unspanned)) == 1 else "history"
        for index, value in enumerate(payload[container_key]):
            describe(claimed.get(index, fallback), value)
    for component, path in extras:
        describe(component, at_path(payload, path))
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
    if previous is None:
        return {"change_reason": "first_request", "prefix_preserved": False,
                "first_different_item": None, "first_different_component": None}
    before, after = previous["_items"], current["_items"]
    first = next((i for i, pair in enumerate(zip(before, after)) if pair[0] != pair[1]), None)
    if first is None and len(before) != len(after):
        first = min(len(before), len(after))
    component = (after[first][0] if first is not None and first < len(after) else
                 before[first][0] if first is not None and first < len(before) else None)
    # Dynamic suffix displacement is expected as completed tool history grows.
    old_prefix = [i for i in before if i[0] != "dynamic"]
    new_prefix = [i for i in after if i[0] != "dynamic"]
    preserved = old_prefix == new_prefix[:len(old_prefix)]
    tools_equal = previous["components"]["tools"] == current["components"]["tools"]
    parameters_equal = previous["parameters_hash"] == current["parameters_hash"]
    dynamic_equal = previous["components"]["dynamic"] == current["components"]["dynamic"]
    reason = ("tools_changed" if not tools_equal else "parameters_changed" if not parameters_equal else
              "prefix_changed" if not preserved else "dynamic_changed" if not dynamic_equal else
              "history_appended" if len(new_prefix) > len(old_prefix) else "unchanged")
    return {"change_reason": reason, "first_different_item": first,
            "first_different_component": "tools" if not tools_equal else component,
            "prefix_preserved": preserved and tools_equal and parameters_equal,
            "dynamic_unchanged": dynamic_equal}

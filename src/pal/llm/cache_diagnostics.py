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
    for message in request.messages:
        component = ("dynamic" if message.prompt_region == PromptRegionIR.ACTIVE_DYNAMIC else
                     "stable" if message.prompt_region == PromptRegionIR.STABLE_SYSTEM else "history")
        span = spans.get(message.message_id)
        if span is None:
            continue
        paths = span.wire_item_paths or span.cache_targets
        for path in paths:
            item = at_path(payload, path)
            digest, size = fingerprint(item)
            items.append((component, digest, size))
            groups[component].append(item)
    components = {key: dict(zip(("hash", "bytes"), fingerprint(value))) for key, value in groups.items()}
    markers = []
    applied = set(encoded.applied_cache_breakpoint_message_ids)
    for span in encoded.message_spans:
        if span.message_id not in applied:
            continue
        for path in span.cache_targets:
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

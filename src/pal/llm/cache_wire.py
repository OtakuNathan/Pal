"""OpenAI cache wire adapter: exact positions, content identity and final audit.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Mapping

from pal.llm.shapes.base import EncodedRequest, finalize_cache_spans
from pal.shared.json_values import thaw_json

CONTROL_FIELDS = ("prompt_cache_breakpoint", "cache_control")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(thaw_json(value), ensure_ascii=False, separators=(",", ":"),
                                     sort_keys=True).encode()).hexdigest()


def at(payload: dict, path: tuple) -> Any:
    try:
        for part in path:
            payload = payload[part]
        return payload
    except (KeyError, IndexError, TypeError):
        return None


def protocol_nodes(payload: dict):
    """Visit wire envelopes only, never tool arguments, schema or user data."""
    yield (), payload
    for root in ("input", "messages", "system", "tools"):
        items = payload.get(root)
        if not isinstance(items, list):
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            yield (root, i), item
            if root == "tools":
                continue
            for field_name in ("content", "output"):
                blocks = item.get(field_name)
                if isinstance(blocks, list):
                    for j, block in enumerate(blocks):
                        if isinstance(block, dict):
                            yield (root, i, field_name, j), block


def clean_request(encoded: EncodedRequest, *, normalize_chat: bool = True, finalize: bool = True) -> EncodedRequest:
    """Normalize known controls before content hashing, including extra_body.

    The transport merges extra_body over payload. Plan against that same input,
    so an override of messages/tools cannot silently invalidate our audit.
    """
    payload = thaw_json(encoded.payload)
    payload.update(thaw_json(encoded.extra_body))
    for _, node in protocol_nodes(payload):
        for key in CONTROL_FIELDS:
            node.pop(key, None)
    for key in ("prompt_cache_options", "prompt_cache_key", "session_id"):
        payload.pop(key, None)
    # Chat instruction strings need a concrete content block before fingerprinting.
    for item in payload.get("messages", ()):
        if normalize_chat and isinstance(item, dict) and isinstance(item.get("content"), str):
            item["content"] = [{"type": "text", "text": item["content"]}]
    spans = []
    for span in encoded.message_spans:
        paths = tuple(path + ("content", 0) if len(path) == 2
                      and path[0] == "messages" and isinstance(at(payload, path), dict)
                      and isinstance(at(payload, path).get("content"), list)
                      else path for path in span.cache_targets)
        spans.append(replace(span, cache_targets=paths))
    result = EncodedRequest(payload, tuple(spans))
    return finalize_cache_spans(result) if finalize else result


def inject_exact(encoded: EncodedRequest, points: tuple, cache_key: str,
                 gateway: bool, *, prepared: bool = False, mode: str = "explicit") -> EncodedRequest:
    clean = encoded if prepared else clean_request(encoded)
    payload = thaw_json(clean.payload)
    spans = {s.message_id: s for s in clean.message_spans}
    applied = []
    for point in points:
        span = spans.get(point.message_id)
        if not span or not span.cache_targets:
            continue
        target = at(payload, point.path or span.cache_targets[-1])
        # Only supported content blocks; never fallback to an earlier block.
        if isinstance(target, dict) and target.get("type") in {
            "input_text", "output_text", "text", "input_image", "image_url",
            "input_audio", "file", "input_file", "refusal",
        }:
            target["prompt_cache_breakpoint"] = {"mode": "explicit"}
            applied.append(point.message_id)
    extra = {"prompt_cache_key": cache_key,
             "prompt_cache_options": {"mode": "explicit" if mode == "explicit" else "implicit", "ttl": "30m"}}
    if gateway:
        extra["session_id"] = cache_key
    return EncodedRequest(payload, clean.message_spans, extra, tuple(applied))


def audit(encoded: EncodedRequest, expected: tuple[tuple, ...], *, mode: str = "explicit") -> tuple[bool, str]:
    payload = thaw_json(encoded.payload)
    payload.update(thaw_json(encoded.extra_body))
    found = []
    valid = payload.get("prompt_cache_options") == {"mode": "explicit" if mode == "explicit" else "implicit", "ttl": "30m"}
    for path, node in protocol_nodes(payload):
        if "cache_control" in node:
            valid = False
        if "prompt_cache_breakpoint" in node:
            found.append(path)
            valid &= node["prompt_cache_breakpoint"] == {"mode": "explicit"}
    valid &= len(found) <= (4 if mode == "explicit" else 3) and set(found) == set(expected) and len(found) == len(set(found))
    return bool(valid), digest({"paths": found, "options": payload.get("prompt_cache_options"),
                               "key": payload.get("prompt_cache_key"),
                               "session": payload.get("session_id")})


@dataclass(frozen=True)
class Boundary:
    message_id: str
    path: tuple
    fingerprint: str
    coordinate: int


def boundary_at(encoded: EncodedRequest, message_id: str, path: tuple) -> Boundary | None:
    """Describe a selected block, including an interior continuity block."""
    from pal.llm.shapes.base import _provider_prefix
    prefix = _provider_prefix(thaw_json(encoded.payload), path)
    if prefix is None or not isinstance(at(encoded.payload, path), Mapping):
        return None
    serialized = json.dumps(prefix, ensure_ascii=False, separators=(",", ":"))
    return Boundary(message_id, path, hashlib.sha256(serialized.encode()).hexdigest(),
                    max(1, (len(serialized) + 3) // 4))

"""Cache addresses carried beside frozen wire items, never inside provider data.

Paths here are relative to ONE container item. Assembly adds its index; merges
and retirement transform block offsets with the same operation as the bytes.
"""
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from pal.llm.shapes.base import EncodedMessageSpan

Path = tuple[str | int, ...]
ItemSpans = tuple[EncodedMessageSpan, ...]


def map_span(span: EncodedMessageSpan, transform: Callable[[Path], Path | None]) -> EncodedMessageSpan:
    return replace(
        span,
        cache_targets=tuple(p for path in span.cache_targets if (p := transform(path)) is not None),
        wire_item_paths=tuple(p for path in span.wire_item_paths if (p := transform(path)) is not None),
        continuity_target=(transform(span.continuity_target) or ()) if span.continuity_target else (),
        cache_prefix_fingerprint="",
        estimated_cache_prefix_tokens=0,
    )


def split_spans(spans: Sequence[EncodedMessageSpan], container: str, count: int) -> list[ItemSpans]:
    result: list[list[EncodedMessageSpan]] = [[] for _ in range(count)]
    for span in spans:
        paths = (*span.cache_targets, *span.wire_item_paths,
                 *((span.continuity_target,) if span.continuity_target else ()))
        indexes = {path[1] for path in paths
                   if len(path) >= 2 and path[0] == container and isinstance(path[1], int)}
        for index in indexes:
            if 0 <= index < count:
                local = map_span(span, lambda p: p[2:] if len(p) >= 2 and p[:2] == (container, index) else None)
                result[index].append(local)
    return [tuple(entries) for entries in result]


def join_spans(per_item: Sequence[ItemSpans], container: str) -> ItemSpans:
    merged: dict[str, EncodedMessageSpan] = {}
    for index, entries in enumerate(per_item):
        for span in entries:
            moved = map_span(span, lambda path: (container, index, *path))
            old = merged.get(span.message_id)
            if old is not None:
                moved = replace(
                    moved,
                    cache_targets=(*old.cache_targets, *moved.cache_targets),
                    wire_item_paths=(*old.wire_item_paths, *moved.wire_item_paths),
                    continuity_target=old.continuity_target or moved.continuity_target,
                )
            merged[span.message_id] = moved
    return tuple(merged.values())


def shift_blocks(entries: ItemSpans, offset: int) -> ItemSpans:
    return tuple(map_span(span, lambda p: ("content", p[1] + offset, *p[2:])
                         if len(p) >= 2 and p[0] == "content" else p) for span in entries)


def retire_spans(entries: ItemSpans, item: Mapping[str, Any],
                 block_spans: Sequence[tuple[str, ...]], kept: set[str]) -> ItemSpans:
    content = item.get("content")
    if not isinstance(content, list) or not block_spans or len(block_spans) != len(content):
        return entries
    indexes = {old: new for new, old in enumerate(
        i for i, span in enumerate(block_spans) if not span or set(span) <= kept)}

    def remap(path: Path) -> Path | None:
        if len(path) >= 2 and path[0] == "content":
            return ("content", indexes[path[1]], *path[2:]) if path[1] in indexes else None
        return path

    result = []
    for span in entries:
        moved = map_span(span, remap)
        if span.cache_targets and not moved.cache_targets:
            continue  # Retired blocks cannot claim their old shared item.
        result.append(moved)
    return tuple(result)


def dump_spans(entries: ItemSpans) -> list[dict[str, Any]]:
    return [dict(message_id=s.message_id, cache_targets=list(s.cache_targets),
                 wire_item_paths=list(s.wire_item_paths), continuity_target=s.continuity_target)
            for s in entries]


def load_spans(entries: Any) -> ItemSpans:
    if not isinstance(entries, (list, tuple)):
        raise ValueError("cache spans must be a list")

    def path(value: Any) -> Path:
        if not isinstance(value, (list, tuple)) or any(
            type(part) not in (str, int) or isinstance(part, int) and part < 0 for part in value
        ):
            raise ValueError("invalid cache span path")
        return tuple(value)

    result = []
    for entry in entries:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("message_id"), str):
            raise ValueError("invalid cache span message")
        result.append(EncodedMessageSpan(
            message_id=entry["message_id"],
            cache_targets=tuple(path(p) for p in entry.get("cache_targets", ())),
            wire_item_paths=tuple(path(p) for p in entry.get("wire_item_paths", ())),
            continuity_target=path(entry.get("continuity_target") or ()),
        ))
    return tuple(result)


def native_spans(shape: str, items: list[dict], message_id: str) -> list[ItemSpans]:
    """Use the codec's target rules on accepted native bytes, without encoding."""
    container = "input" if shape == "openai_response" else "messages"
    targets = ()
    if shape == "openai_completion":
        from pal.llm.shapes.openai_completion import _chat_message_cache_targets
        targets = tuple(path for i in range(len(items))
                        for path in _chat_message_cache_targets(items, i))
    elif shape == "anthropic_messages" and items:
        from pal.llm.shapes.anthropic_messages import _last_message_cache_targets
        targets = _last_message_cache_targets(items)
    span = EncodedMessageSpan(
        message_id, targets, wire_item_paths=tuple((container, i) for i in range(len(items))),
    )
    return split_spans((span,), container, len(items))

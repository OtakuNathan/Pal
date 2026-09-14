"""Conservative normalization shared by compact and worker memory proposals."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

MAX_MEMORY_CANDIDATES = 5
CANDIDATE_FIELDS = frozenset({"kind", "title", "summary", "source_excerpt", "search_text",
    "why_durable", "confidence", "task_id", "topics", "star", "canonical_key"})
STAR_FIELDS = ("situation", "task", "action", "result")


@dataclass(frozen=True)
class MemoryProposalBatch:
    batch_id: str
    candidates: tuple[dict[str, Any], ...]
    source: dict[str, Any] = field(default_factory=dict)


def normalize_memory_candidates(value, *, limit=MAX_MEMORY_CANDIDATES):
    diagnostics = []
    if value is None:
        return [], diagnostics
    if not isinstance(value, list):
        return [], ["memory_candidates:invalid_array"]
    result = []
    for index, raw in enumerate(value):
        path = f"memory_candidates[{index}]"
        try:
            item = normalize_candidate(raw)
        except (ValueError, TypeError) as exc:
            diagnostics.append(f"{path}:{exc}")
            continue
        if isinstance(raw, dict) and set(raw) - CANDIDATE_FIELDS:
            diagnostics.append(f"{path}:extra_fields_removed")
        if limit is not None and len(result) >= limit:
            diagnostics.append(f"{path}:over_candidate_limit")
            continue
        result.append(item)
    return result, diagnostics


def normalize_candidate(raw):
    if not isinstance(raw, dict):
        raise ValueError("invalid_object")
    item = {key: deepcopy(value) for key, value in raw.items() if key in CANDIDATE_FIELDS}
    if isinstance(item.get("kind"), str):
        item["kind"] = item["kind"].strip().lower()
    if item.get("kind") not in {"fact", "case"}:
        raise ValueError("kind:invalid")
    if not item.get("source_excerpt") and isinstance(item.get("search_text"), str):
        item["source_excerpt"] = item["search_text"]
    for key in ("title", "summary", "source_excerpt"):
        if not isinstance(item.get(key), str) or not item[key].strip():
            raise ValueError(f"{key}:required_string")
    for key in ("why_durable", "confidence", "task_id", "search_text", "canonical_key"):
        if item.get(key) is None:
            item.pop(key, None)
        elif not isinstance(item[key], str):
            raise ValueError(f"{key}:invalid_string")
    topics = item.get("topics")
    if topics is None:
        item.pop("topics", None)
    else:
        topics = [topics] if isinstance(topics, str) else topics
        if not isinstance(topics, list) or any(not isinstance(topic, str) for topic in topics):
            raise ValueError("topics:invalid_array")
        item["topics"] = list(dict.fromkeys(topic.strip() for topic in topics if topic.strip()))
    if item["kind"] == "case":
        star = item.get("star")
        if not isinstance(star, dict) or not all(isinstance(star.get(key), str) and star[key].strip() for key in STAR_FIELDS):
            raise ValueError("star:required_fields")
        item["star"] = {key: star[key] for key in STAR_FIELDS}
    elif item.get("star") not in (None, {}, ""):
        raise ValueError("star:not_allowed_for_fact")
    else:
        item.pop("star", None)
    return item

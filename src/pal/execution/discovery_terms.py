"""Word matching for capability discovery, independent of memory retrieval."""
from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache

from jsonschema import Draft202012Validator

from pal.shared.text_search import jieba_search_terms


@lru_cache(maxsize=4096)
def tool_search_terms(text: str) -> tuple[str, ...]:
    # The shared tokenizer deliberately retains identifiers. Tool discovery also
    # needs their individual words: install must never match uninstall.
    return tuple(dict.fromkeys(
        word
        for term in jieba_search_terms(text.lower())
        for word in re.split(r"[_.-]+", term)
        if word
    ))


def discovery_vocabulary(guidance, schema) -> tuple[str, ...]:
    """Only explicitly selected operation fields contribute enum words.

    Resolve local schema references; never index arbitrary descriptions, defaults,
    formats or other value enums. Invalid selections fail at registration.
    """
    words = set(guidance.search_objects) | set(guidance.search_terms)

    def enums(node, seen=frozenset()):
        if not isinstance(node, Mapping):
            return []
        values = list(node.get("enum", ()))
        ref = node.get("$ref")
        if ref:
            if not ref.startswith("#/") or ref in seen:
                return []
            target = schema
            for part in ref[2:].split("/"):
                target = target.get(part.replace("~1", "/").replace("~0", "~"), {})
            values.extend(enums(target, seen | {ref}))
        if "const" in node:
            values.append(node["const"])
        for key in ("anyOf", "oneOf", "allOf"):
            for child in node.get(key, ()):
                values.extend(enums(child, seen))
        return [value for value in values if isinstance(value, str)]

    for field in guidance.search_enum_fields:
        node = schema.get("properties", {}).get(field, {})
        validator = Draft202012Validator(schema).evolve(schema=node)
        values = [value for value in enums(node) if validator.is_valid(value)]
        if not values:
            raise ValueError(f"search_enum_fields: {field!r} must name a string enum input property")
        for value in values:
            words.update(tool_search_terms(value))
    return tuple(sorted(words))

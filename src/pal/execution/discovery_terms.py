"""Word matching for capability discovery, independent of memory retrieval."""
from __future__ import annotations

import re
from functools import lru_cache

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

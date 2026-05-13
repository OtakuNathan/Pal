from __future__ import annotations

import re
from functools import lru_cache
from typing import Iterable

import jieba


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]+|[\u4e00-\u9fff]+")
_STRIP_CHARS = ".,!?;:'\"()[]{}<>`~!@#$%^&*-_=+|\\/，。！？；：（）【】《》、"


def warmup_jieba() -> None:
    jieba.initialize()


def normalize_search_text(text: str) -> str:
    return " ".join(part.strip() for part in str(text or "").splitlines() if part.strip()).strip()


def jieba_fts_text(*parts: object) -> str:
    text = normalize_search_text(" ".join(str(part or "") for part in parts if str(part or "").strip()))
    if not text:
        return ""
    terms = jieba_search_terms(text)
    return " ".join(terms)


def jieba_search_terms(text: str) -> tuple[str, ...]:
    normalized = normalize_search_text(text)
    if not normalized:
        return ()
    return _cached_jieba_search_terms(normalized)


@lru_cache(maxsize=4096)
def _cached_jieba_search_terms(normalized: str) -> tuple[str, ...]:
    warmup_jieba()
    terms: list[str] = []
    for chunk in _TOKEN_RE.findall(normalized):
        chunk = _sanitize_search_term(chunk)
        if not chunk:
            continue
        if _CJK_RE.search(chunk):
            terms.extend(_sanitize_search_term(term) for term in jieba.cut_for_search(chunk))
        else:
            terms.append(chunk.lower())
    return _dedupe_terms(term for term in terms if term)


def compile_jieba_fts_queries(text: str) -> list[tuple[str, float]]:
    terms = jieba_search_terms(text)
    if not terms:
        return []

    queries: list[tuple[str, float]] = []
    if len(terms) > 1:
        queries.append((_quote_fts_phrase(terms), 3.0))
        for window_size, weight in ((4, 2.6), (3, 2.2), (2, 1.8)):
            if len(terms) < window_size:
                continue
            for index in range(len(terms) - window_size + 1):
                queries.append((_quote_fts_phrase(terms[index : index + window_size]), weight))
        queries.append((" OR ".join(_quote_fts_term(term) for term in terms), 0.8))
    else:
        queries.append((_quote_fts_term(terms[0]), 3.0))

    unique: list[tuple[str, float]] = []
    seen: set[str] = set()
    for query, weight in queries:
        compiled = str(query or "").strip()
        if not compiled or compiled in seen:
            continue
        seen.add(compiled)
        unique.append((compiled, weight))
    return unique


def _quote_fts_phrase(terms: Iterable[str]) -> str:
    phrase = " ".join(str(term or "").replace('"', '""').strip() for term in terms if str(term or "").strip())
    return f'"{phrase}"' if phrase else ""


def _quote_fts_term(term: str) -> str:
    escaped = str(term or "").replace('"', '""').strip()
    return f'"{escaped}"' if escaped else ""


def _sanitize_search_term(term: str) -> str:
    return str(term or "").strip().strip(_STRIP_CHARS)


def _dedupe_terms(terms: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for term in terms:
        normalized = str(term or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)

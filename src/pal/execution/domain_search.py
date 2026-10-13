"""Purpose retrieval over filtered visibility, narrowed by an explicit owner."""
from __future__ import annotations

from collections import Counter
from math import log
from typing import Any

from pal.execution.discovery_terms import (
    PURPOSE_STOP_WORDS, _one_edit_apart, normalize_domain, purpose_terms,
    resolve_search_domain, search_domain_catalog, search_facets, tool_domain, tool_search_terms,
)
from pal.shared.tool_routing import TOOL_DISCOVERY_STOP_HINT


_INDEX_FIELDS = frozenset({
    "search_objects", "search_terms", "search_auxiliary_terms", "search_term_rules",
    "search_scope_terms", "search_intents",
    "search_domain_aliases", "search_domain_description",
})


def _noun_form(word: str) -> str:
    # Generic noun-number normalization, never a business synonym expansion.
    if word.isascii() and len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if word.isascii() and len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _document(record: dict[str, Any], query: set[str]) -> set[str]:
    words = set(purpose_terms(str(record.get("purpose") or "")))
    # Older source catalogs contain the merged discovery vocabulary. Remove
    # object-only entries there; current generations retain the authored terms.
    auxiliary = record.get("search_auxiliary_terms")
    if auxiliary is None:
        auxiliary = set(record.get("search_terms", ())) - set(record.get("search_objects", ()))
    for phrase in auxiliary:
        words.update(tool_search_terms(str(phrase)))
    for phrase in record.get("search_intents", ()):
        words.update(purpose_terms(str(phrase)))
    for rule in record.get("search_term_rules", ()):
        if query.intersection(rule["requires_any"]):
            for phrase in rule["terms"]:
                words.update(tool_search_terms(str(phrase)))
    return {_noun_form(word) for word in words - PURPOSE_STOP_WORDS}


def _rank(records: dict[str, Any], words: set[str], *, domain: str) -> list[dict[str, Any]]:
    owner_words = set(tool_search_terms(domain))
    query = words - PURPOSE_STOP_WORDS - owner_words
    normalized = {_noun_form(word) for word in query}
    if not normalized:
        return []
    documents = {alias: _document(record, words) - owner_words for alias, record in records.items()}
    frequencies = Counter(word for document in documents.values() for word in document)
    count = len(documents)
    average = sum(map(len, documents.values())) / max(1, count) or 1
    ranked = []
    for alias, document in documents.items():
        matched = normalized & document
        if not matched:
            continue
        if not domain and len(matched) < 2:
            # Across owners, a single common verb/context word is too weak to
            # return a callable contract. No full-query coverage is required.
            continue
        score = sum(log(1 + (count - frequencies[word] + .5) / (frequencies[word] + .5))
                    * 2.2 / (1 + 1.2 * (.25 + .75 * len(document) / average))
                    for word in sorted(matched))
        original_matches = {word for word in query if _noun_form(word) in matched}
        ranked.append({"alias": alias, "score": max(1, round(score * 1_000_000)),
                       "matched_terms": sorted(original_matches),
                       "unmatched_terms": sorted(query - original_matches)})
    return sorted(ranked, key=lambda row: (-row["score"], row["alias"]))


def search_domain(generation: Any, args: dict[str, Any]) -> dict[str, Any]:
    """An empty owner supports broad purpose fallback, without global browsing."""
    query = str(args.get("query") or "").strip().lower()
    domain = resolve_search_domain(str(args.get("domain") or ""), search_domain_catalog(generation.search_records))
    namespace = str(args.get("namespace") or "").strip().lower()
    namespace = {"inspect": "introspection", "action": "operation"}.get(namespace, namespace)
    filters = {"namespace": namespace, "family": str(args.get("family") or "").strip().lower(),
               "module_id": str(args.get("module_name") or args.get("module_id") or "").strip().lower()}
    tags = {str(tag).strip().lower() for tag in (args.get("tags") or ())}
    try:
        limit = min(5, max(1, int(args.get("top_k") or args.get("limit") or 3)))
    except (TypeError, ValueError):
        limit = 3
    records = {}
    domains = set()
    for alias, record in generation.search_records.items():
        owner = record.get("domain") or tool_domain(alias, str(record.get("module_id") or ""))
        domains.add(owner)
        if (not domain or owner == domain) and all(not value or str(record.get(key) or "").lower() == value
                                   for key, value in filters.items()) and tags.issubset(str(tag).lower() for tag in record.get("tags", ())):
            records[alias] = record
    applied = {key: value for key, value in {"query": query, "domain": domain, **filters,
                                          "tags": sorted(tags)}.items() if value}
    result = {"hits": [], "total_count": 0, "returned_count": 0, "top_k": limit,
              "truncated": False, "omitted_weaker_count": 0, "applied_filters": applied}
    browse = args.get("directory_offset") is not None or not query
    words = set(tool_search_terms(query))
    requested_alias = next((alias for alias in sorted(generation.search_records) if alias.lower() == query), None)
    excluded_alias = requested_alias is not None and requested_alias not in records
    ranked = [] if browse or excluded_alias else _rank(records, words, domain=domain)
    exact = next((alias for alias in sorted(records) if alias.lower() == query), None)
    if not browse and exact is not None:
        ranked = [{"alias": exact, "score": 1, "matched_terms": sorted(words), "unmatched_terms": []}]

    def contract(row: dict[str, Any]) -> dict[str, Any]:
        return {**{key: value for key, value in records[row["alias"]].items() if key not in _INDEX_FIELDS},
                **{key: value for key, value in row.items() if key != "retrieval_sources"}}

    if ranked:
        result.update(hits=[contract(row) for row in ranked[:limit]], total_count=len(ranked),
                      returned_count=min(len(ranked), limit), truncated=len(ranked) > limit)
        result["usage_hint"] = (
            "Candidates are ranked by positive purpose and tool-authored search terms "
            + ("within this domain. " if domain else "across visible tools and the supplied filters. ") +
            "Lexical overlap is not proof that every requested feature exists. Compare the full contract "
            "with the original task, including unmatched terms; invoke only if it supports the task. "
            "No extra read_tool is needed when the contract is complete."
        )
    elif domain and not browse and not excluded_alias and records:
        # Spelling-only recovery after lexical retrieval misses. No central
        # action/object synonym list or cross-domain correction is applied.
        known = set().union(*(_document(record, words) for record in records.values()))
        alternatives = {}
        for word in sorted(words - PURPOSE_STOP_WORDS - set(tool_search_terms(domain)) - known):
            for target in sorted(known):
                if _one_edit_apart(word, target):
                    for row in _rank(records, words - {word} | {target}, domain=domain):
                        alias = row["alias"]
                        original = _document(records[alias], words)
                        matches = {term for term in words - PURPOSE_STOP_WORDS - {domain}
                                   if _noun_form(term) in original}
                        candidate = {**row, "purpose": records[alias].get("purpose", ""),
                                     "matched_terms": sorted(matches),
                                     "unmatched_terms": sorted(words - PURPOSE_STOP_WORDS - {domain} - matches),
                                     "retrieval_sources": ["fuzzy"]}
                        previous = alternatives.get(alias)
                        if previous is None or candidate["score"] > previous["score"]:
                            alternatives[alias] = candidate
        suggestions = sorted(alternatives.values(), key=lambda row: (-row["score"], row["alias"]))[:min(3, limit)]
        if suggestions:
            result["suggestions"] = [{**row, "contract": contract(row)} for row in suggestions]
            result["usage_hint"] = "Spelling-recovery candidates include full contracts. Verify the original request before invoking; unmatched words do not establish supported features."
    if args.get("facets"):
        result["facets"] = search_facets(records[row["alias"]] for row in ranked)
    if domain and domain not in domains:
        result["available_domains"] = sorted(domains - {""})
        result["usage_hint"] = "Unknown or unavailable domain. No other bucket was searched; choose available_domains or explicitly omit domain for broad discovery."
    elif domain and not result["hits"] and not result.get("suggestions") and records:
        offset = max(0, int(args.get("directory_offset") or 0))
        page = []
        for alias in sorted(records)[offset:offset + 16]:
            purpose = str(records[alias].get("purpose") or "")
            page.append({"alias": alias, "purpose": purpose if len(purpose) <= 160 else purpose[:159].rstrip() + "…",
                         **({"purpose_truncated": True} if len(purpose) > 160 else {})})
        next_offset = offset + len(page)
        result["directory"] = {"domain": domain, "tools": page, "total_count": len(records),
                               "offset": offset, "page_size": 16,
                               "next_offset": next_offset if next_offset < len(records) else None}
        result["usage_hint"] = "Domain directory requested." if browse else "No positive query evidence; directory entries are navigation, not matches."
        result["usage_hint"] += " Read a plausible contract before invocation; use next_offset for more, or query the purpose within the same domain."
    elif not result["hits"] and not result.get("suggestions"):
        result.setdefault("usage_hint", "No matching tools within this domain and supplied filters.")
    if result.get("usage_hint"):
        result["usage_hint"] += " " + TOOL_DISCOVERY_STOP_HINT
    return result

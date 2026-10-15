"""Word matching for capability discovery, independent of memory retrieval."""
from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator

from pal.shared.text_search import jieba_search_terms


# Public search-domain words, not a promise that a domain is currently mounted.
# Aliases carry these words in varying positions; never assume the first word.
SEARCH_DOMAINS = frozenset({
    "core", "execution", "llm", "channel", "browser", "web", "memory",
    "skill", "plugin", "mcp", "lsp", "artifact", "proactive", "checklist",
    "bunshin", "identity", "failure", "file", "shell",
})

DOMAIN_ALIASES = {
    **{name: name for name in SEARCH_DOMAINS},
    "large language model": "llm", "large language models": "llm",
    "language server protocol": "lsp", "model context protocol": "mcp",
    "plugins": "plugin", "skills": "skill", "files": "file",
    "channels": "channel", "artifacts": "artifact", "web search": "web",
}
MODULE_DOMAINS = {
    **{name: name for name in SEARCH_DOMAINS},
    "plugins": "plugin", "web_fetch": "browser", "web_search": "web",
    "control": "core",
}
ALIAS_DOMAINS = {
    **{name: "file" for name in ("read_file", "write_file", "edit_file")},
    "run_shell": "shell",
}


def normalize_domain(value: str) -> str:
    key = " ".join(re.split(r"[\s_-]+", value.strip().lower()))
    return DOMAIN_ALIASES.get(key, key)


def tool_domain(alias: str, module_id: str = "", *, declared_domain: str = "") -> str:
    """Operation ownership, not arbitrary target names mentioned in guidance."""
    if declared_domain:
        return normalize_domain(declared_domain)
    if alias in ALIAS_DOMAINS:
        return ALIAS_DOMAINS[alias]
    owner = module_id.split(".", 1)[0]
    if owner == "l3":
        return "memory"
    if owner in MODULE_DOMAINS:
        return MODULE_DOMAINS[owner]
    # Definition-only fixtures and unusual providers may lack a canonical owner.
    domains = {normalize_domain(word) for word in tool_search_terms(alias)} & SEARCH_DOMAINS
    if len(domains) == 1:
        return next(iter(domains))
    return normalize_domain(module_id) if module_id else ""


def search_domain_catalog(records: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Visible owner buckets and authored aliases; collisions reject publication."""
    catalog: dict[str, dict[str, Any]] = {}
    for alias, record in sorted(records.items()):
        domain = record.get("domain") or tool_domain(alias, str(record.get("module_id") or ""))
        if not domain:
            continue
        entry = catalog.setdefault(domain, {"aliases": set(), "descriptions": set(), "tool_count": 0})
        entry["tool_count"] += 1
        entry["aliases"].update(record.get("search_domain_aliases", ()))
        description = str(record.get("search_domain_description") or "").strip()
        if description:
            entry["descriptions"].add(description)
    owners = {**DOMAIN_ALIASES, **{domain: domain for domain in catalog}}
    for domain, entry in catalog.items():
        normalized = set()
        for alias in sorted(entry["aliases"]):
            key = normalize_domain(alias)
            if key in owners and owners[key] != domain:
                raise ValueError(f"Search domain alias {alias!r} conflicts with owner {owners[key]!r}")
            owners[key] = domain
            if key != domain:
                normalized.add(key)
        entry["aliases"] = sorted(normalized)
        entry["description"] = "; ".join(sorted(entry.pop("descriptions")))
    return catalog


def resolve_search_domain(value: str, catalog: Mapping[str, Any]) -> str:
    domain = normalize_domain(value)
    for owner, entry in catalog.items():
        if domain in entry["aliases"]:
            return owner
    return domain

# These are recovery alternatives, never unconditional strict-match vocabulary.
FUZZY_WORD_GROUPS = (
    frozenset({"list", "enumerate"}), frozenset({"read", "view"}),
    frozenset({"inspect", "status", "details", "information", "info"}),
    frozenset({"delete", "remove", "erase", "forget"}),
    frozenset({"edit", "modify", "update"}),
    frozenset({"pause", "suspend", "disable"}),
    frozenset({"resume", "enable"}),
    frozenset({"reload", "reattach"}),
    frozenset({"attach", "connect"}), frozenset({"detach", "disconnect"}),
    frozenset({"usage", "spending"}), frozenset({"content", "contents"}),
)


def _one_edit_apart(left: str, right: str) -> bool:
    """Bounded spelling recovery, including one adjacent transposition."""
    if min(len(left), len(right)) < 4 or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        differences = [i for i, (a, b) in enumerate(zip(left, right)) if a != b]
        return len(differences) == 1 or (
            len(differences) == 2 and differences[1] == differences[0] + 1
            and left[differences[0]] == right[differences[1]]
            and left[differences[1]] == right[differences[0]])
    shorter, longer = sorted((left, right), key=len)
    return any(longer[:i] + longer[i + 1:] == shorter for i in range(len(longer)))


def fuzzy_query_matches(alias: str, terms: set[str], vocabulary: set[str],
                        missing: set[str], known_words: set[str], *, domain: str = "") -> bool:
    """Recover one explainable word inside an explicit, unchanged domain.

    Never drop an arbitrary unknown word: it may name a domain or a constraint.
    Domain spellings themselves are not automatically corrected.
    """
    def domains_in(words):
        return {word if word in SEARCH_DOMAINS else word[:-1]
                for word in words if word in SEARCH_DOMAINS
                or (word.endswith("s") and word[:-1] in SEARCH_DOMAINS)}

    domains = {domain} if domain else domains_in(terms)
    alias_domains = domains_in(tool_search_terms(alias))
    if not domains or (not domain and not domains.issubset(alias_domains)) or len(missing) != 1:
        return False
    word = next(iter(missing))
    if domains_in({word}):
        return False
    targets = {target for target in vocabulary if not domains_in({target})}
    if any(word in group and group.intersection(targets) for group in FUZZY_WORD_GROUPS):
        return True
    if domains == {"browser"} and word in {"page", "webpage"}:
        return True
    return word not in known_words and any(_one_edit_apart(word, target) for target in targets)


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


def discovery_vocabulary(guidance, schema, *, include_objects: bool = True) -> tuple[str, ...]:
    """Only explicitly selected operation fields contribute enum words.

    Resolve local schema references; never index arbitrary descriptions, defaults,
    formats or other value enums. Invalid selections fail at registration.
    """
    words = set(guidance.search_terms)
    if include_objects:
        words.update(guidance.search_objects)

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


def validate_search_rules(alias, guidance, vocabulary) -> None:
    """Reject bypasses and conditions that would depend on another rule."""
    base = set(tool_search_terms(alias)) | set(vocabulary)
    for rule in guidance.search_term_rules:
        if base.intersection(rule.terms):
            raise ValueError(f"conditional search terms for {alias!r} must not be unconditional")
        if not set(rule.requires_any).issubset(base):
            raise ValueError(f"search rule conditions for {alias!r} must name indexed words")


def query_vocabulary(record, terms) -> set[str]:
    """Enable conditional words using only the original, unexpanded query."""
    words = set(record.get("search_terms", record.get("search_objects", ())))
    for rule in record.get("search_term_rules", ()):
        if terms.intersection(rule["requires_any"]):
            words.update(rule["terms"])
    return words


# Function words carry no capability evidence. Keep negation out of retrieval.
PURPOSE_STOP_WORDS = frozenset('a an the to of for in on with and or one by from its this that as at into'.split())
ACTION_WORDS = frozenset('list read inspect set send create upsert update edit delete remove forget attach detach reload restart rescan enable disable install uninstall check click reset write search find run execute submit archive pause resume clear show fetch connect disconnect switch'.split())

# Unmatched words from this set describe diagnostic context, not a capability.
# They lower rank and cannot supply the action/object evidence needed for entry.
# Operational qualifiers such as encrypted, remote, auth and quota stay strict.
INSPECTION_ACTIONS = frozenset({'inspect', 'check', 'show', 'observe', 'get', 'read', 'view'})
QUERY_MODIFIERS = frozenset({'current', 'local', 'exact', 'valid', 'requested', 'specific'})
INSPECTION_CONTEXT_WORDS = frozenset({
    'configuration', 'config', 'runtime', 'manager', 'backend', 'connection',
    'module', 'service', 'system', 'diagnostic', 'diagnostics', 'information',
    'info', 'details', 'health', 'status', 'state', 'tool', 'tools', 'hub', 'host',
})


def partial_query_match(terms: set[str], evidence: set[str], action_evidence: set[str],
                        *, domain: str, owner: str, blocked: set[str]) -> bool:
    """Require operation/object anchors before softening diagnostic context.

    Neither context words nor a domain alone establish support. Unknown objects,
    unsupported actions, negation and inactive conditional terms still block
    admission. This also handles structured evidence without new purpose words.
    """
    meaningful = terms - PURPOSE_STOP_WORDS
    actions = terms & ACTION_WORDS
    modifiers = QUERY_MODIFIERS
    if actions & INSPECTION_ACTIONS:
        modifiers = modifiers | INSPECTION_CONTEXT_WORDS
    matched = evidence & meaningful
    requested_operations = terms & action_evidence
    return (
        len(matched) >= 2
        and bool(matched - ACTION_WORDS - QUERY_MODIFIERS - requested_operations - {owner})
        and bool(requested_operations)
        and not (actions - action_evidence)
        and not (meaningful - evidence - modifiers)
        and not (terms & blocked)
        and not terms.intersection({'not', 'never', 'without'})
        and (bool(domain) or not (terms & SEARCH_DOMAINS) - evidence)
    )


@lru_cache(maxsize=4096)
def purpose_terms(purpose: str) -> frozenset[str]:
    """Index affirmative purpose clauses, never references to other tool aliases.

    Guidance's use/avoid examples and recovery instructions are deliberately not
    supplied here. Conservative clause removal avoids learning negated abilities.
    """
    # Lower-case identifiers can refer to other callable aliases. Preserve
    # uppercase state constants such as TRIAGE_REQUIRED as object vocabulary.
    purpose = re.sub(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b', ' ', purpose)
    clauses = re.split(r'[;!?]|\.(?:\s|$)|\b(?:but|instead|rather than)\b', purpose.lower())
    positive = []
    for clause in clauses:
        clause = re.split(r'\bwithout\b', clause)[0]
        if re.search(r"\b(?:not|never|cannot|don't|doesn't|preserves?|retains?)\b", clause):
            continue
        clause = re.split(r'\b(?:retaining|preserving|keeping)\b', clause)[0]
        positive.append(clause)
    return frozenset(tool_search_terms(' '.join(positive))) - PURPOSE_STOP_WORDS


def purpose_relevance(records, terms: set[str]) -> dict[str, tuple[float, set[str]]]:
    """Small binary-term BM25 index; bounded cache stores only tokenized text.

    Statistics use the caller's visible domain bucket. Query/registry ordering
    cannot change scores. No embeddings, external index, or dependencies.
    """
    from collections import Counter
    from math import log

    documents = {alias: purpose_terms(str(item.get('purpose') or '')) for alias, item in records.items()}
    frequencies = Counter(word for words in documents.values() for word in words)
    count = len(documents)
    average = sum(map(len, documents.values())) / max(1, count) or 1
    query = terms - PURPOSE_STOP_WORDS
    result = {}
    for alias, words in documents.items():
        matched = query & words
        score = sum(log(1 + (count - frequencies[word] + .5) / (frequencies[word] + .5))
                    * 2.2 / (1 + 1.2 * (.25 + .75 * len(words) / average))
                    for word in sorted(matched))
        result[alias] = (score, set(matched))
    return result


def purpose_action_evidence(alias: str, purpose: str, terms: set[str]) -> set[str]:
    """Allow compound purpose actions only when their main operation is requested.

    'enable and attach' supports both, but 'fetch' inside network observation
    does not turn that tool into a page fetcher. Negative clauses stay excluded.
    """
    opening = re.sub(r"^(?:explicitly|terminally|durably)\s+", "", purpose.strip().lower())
    primary = set(tool_search_terms(opening.split(" ", 1)[0]))
    alias_action = set(tool_search_terms(alias.split("_", 1)[0]))
    positive = set(purpose_terms(purpose))
    if terms & (primary | alias_action):
        return (positive & ACTION_WORDS) | (primary & positive)
    return primary & positive


def retrieval_candidates(records, terms: set[str], *, domain: str, fuzzy_matches) -> list[dict]:
    """Fuse bounded lexical routes without weakening visibility or ownership.

    Intents are affirmative, reviewed discovery metadata. A complete intent
    match can retrieve a contract, but unsupported actions, conditional terms
    and negation still reject it. Fuzzy evidence is supplied by the existing
    domain-scoped matcher. No model, vector index or external service is used.
    """
    meaningful = terms - PURPOSE_STOP_WORDS
    if not meaningful or terms.intersection({'not', 'never', 'without'}):
        return []
    purpose_scores = purpose_relevance(records, terms)
    intent_scores = purpose_relevance({
        alias: {'purpose': '. '.join(record.get('search_intents', ()))}
        for alias, record in records.items()
    }, terms)
    evidence = {}
    for alias, record in records.items():
        owner = record.get('domain') or tool_domain(alias, str(record.get('module_id') or ''))
        structured = set(tool_search_terms(alias)) | query_vocabulary(record, terms)
        positive = set(purpose_terms(str(record.get('purpose') or '')))
        blocked = {word for rule in record.get('search_term_rules', ())
                   if not terms.intersection(rule['requires_any']) for word in rule['terms']}
        if terms & blocked:
            continue
        actions = (structured & ACTION_WORDS) | purpose_action_evidence(
            alias, str(record.get('purpose') or ''), terms)
        modifiers = QUERY_MODIFIERS
        if terms & INSPECTION_ACTIONS:
            modifiers = modifiers | INSPECTION_CONTEXT_WORDS
        for phrase in record.get('search_intents', ()):
            intent = set(purpose_terms(phrase))
            # Noun number in a reviewed intent is not a new capability. Keep
            # this expansion out of strict/conditional query vocabulary.
            for word in tuple(intent):
                if len(word) > 4 and word.endswith('ies'):
                    intent.add(word[:-3] + 'y')
                elif len(word) > 3 and word.endswith('s') and not word.endswith(('ss', 'us', 'is')):
                    intent.add(word[:-1])
            matched = meaningful & (structured | positive | intent)
            # An authored intent can name an object differently, but its leading
            # verb, the owner and generic diagnostic words are not object anchors.
            leading = set(tool_search_terms(phrase.split(' ', 1)[0])) if ' ' in phrase else set()
            objects = (structured | positive | (intent - leading)) - ACTION_WORDS - modifiers - {owner}
            if (len(matched) >= 2 and bool(matched & objects)
                    and (domain or len(matched - ACTION_WORDS - modifiers - {owner}) >= 2)
                    and not ((terms & ACTION_WORDS) - actions - intent)
                    and not (meaningful - matched - modifiers)
                    and (domain or not (terms & SEARCH_DOMAINS) - matched)):
                previous = evidence.get(alias, set())
                evidence[alias] = previous | matched
    fuzzy = {item['alias']: item for item in fuzzy_matches if item['alias'] in records}
    pool = set(evidence) | set(fuzzy)
    if not pool:
        return []
    # Each independent ranking contributes an ordinal score. Lexical score
    # scales need no hand normalization; alias breaks ties deterministically.
    routes = {
        'structured': sorted(pool, key=lambda a: (-len(meaningful & (
            set(tool_search_terms(a)) | query_vocabulary(records[a], terms))), a)),
        'purpose': sorted((a for a in pool if purpose_scores[a][0] > 0),
                          key=lambda a: (-purpose_scores[a][0], a)),
        'intent': sorted((a for a in evidence if intent_scores[a][0] > 0),
                         key=lambda a: (-intent_scores[a][0], a)),
        'fuzzy': [item['alias'] for item in fuzzy_matches if item['alias'] in pool],
    }
    scores = {alias: 0.0 for alias in pool}
    sources = {alias: [] for alias in pool}
    for source, ranking in routes.items():
        for rank, alias in enumerate(ranking, 1):
            scores[alias] += 1 / (60 + rank)
            sources[alias].append(source)
    result = []
    for alias in sorted(pool, key=lambda a: (-scores[a], a)):
        matched = evidence.get(alias, set()) | set(fuzzy.get(alias, {}).get('matched_terms', ()))
        result.append({'alias': alias, 'purpose': records[alias].get('purpose', ''),
                       'matched_terms': sorted(matched), 'unmatched_terms': sorted(meaningful - matched),
                       'retrieval_sources': sources[alias], 'score': round(scores[alias] * 1000000)})
    return result


def search_facets(records: Any) -> dict[str, Any]:
    counts: dict[str, dict[str, int]] = {
        "namespaces": {},
        "modules": {},
        "families": {},
    }
    for record in records:
        for bucket, key, field_name in (
            ("namespaces", "namespace", "namespace"),
            ("modules", "module_id", "module_id"),
            ("families", "family", "family"),
        ):
            value = str(record.get(field_name) or "")
            if not value:
                continue
            counts[bucket][value] = counts[bucket].get(value, 0) + 1
    return {
        "namespaces": [
            {"namespace": key, "count": count}
            for key, count in sorted(counts["namespaces"].items())
        ],
        "modules": [
            {"module_id": key, "count": count}
            for key, count in sorted(counts["modules"].items())
        ],
        "families": [
            {"family": key, "count": count}
            for key, count in sorted(counts["families"].items())
        ],
    }

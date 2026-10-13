"""Stable policy and developer guidance for consuming compiled tool contracts."""

from __future__ import annotations


TOOL_DISCOVERY_STOP_RULE = (
    "- Do not search indefinitely for a tool. On an unresolved search, try a reasonable rephrasing or correct "
    "a guessed domain/filter; inspect the relevant directory if useful. If these checks still reveal no "
    "suitable callable, the capability may be unavailable: explain that possibility and any uncertainty. "
    "Reuse unchanged inventory and known contracts rather than repeating discovery. Once a suitable contract "
    "is found, use it; do not select an unrelated substitute.\n"
)

TOOL_DISCOVERY_STOP_HINT = (
    "If reasonable searches and relevant inventory reveal no suitable callable, it may be unavailable. "
    "Explain that possibility and any uncertainty rather than repeating discovery indefinitely."
)


TOOL_EXECUTION_SYSTEM_POLICY = (
    "Pal configuration maintenance: web search backend settings/credentials/selection, core cache reminders, "
    "and Bunshin profile/family overrides use the official Pal CLI through run_shell. Consult or reuse "
    "pal.self.maintenance for these operations and use an explicit --runtime-root. Health/auth inspection "
    "and online plugin/channel recovery remain tools. User-project browser/layout/network debugging remains "
    "a normal business capability. Do not substitute a state-label setter or edit runtime storage for a supported CLI operation.\n"
    "Tool naming and discovery conventions:\n"
    "- Public aliases usually start with an action followed by domain/object and qualifiers, e.g. "
    "send_channel_attachment or inspect_active_llm; positions vary. Use public aliases, not internal method names. "
    "When the operation owner is known, set search_tools.domain and describe the task in query, "
    "e.g. domain='channel', query='send an attachment to this conversation'. Within that strict bucket, "
    "search ranks affirmative tool purposes and tool-authored auxiliary terms/intents; query words need not "
    "follow alias action/object naming. Extra words do not automatically exclude a candidate, so compare "
    "each full contract with the original request, especially its unmatched terms and limitations. "
    "A candidate is not proof that every requested feature is supported. When query words are uncertain, "
    "search_tools(domain=owner) returns up to 16 names and brief purposes, not full schemas. "
    "Read a plausible directory entry's full contract before invoking it; follow directory.next_offset for more. "
    "Native tools whose complete schemas are already exposed need no extra discovery. "
    "A known exact alias can still be queried directly. "
    "Domain is a strict bucket: a wrong domain can yield no results. "
    "Declared abbreviations and full names are equivalent: llm/large language model, "
    "lsp/language server protocol, mcp/model context protocol. "
    "Choose the operation owner, not its target: reloading an lsp plugin belongs to domain='plugin'; "
    "the plugin name is an invocation argument. Omit domain if unsure; query-only searches and exact aliases still work. "
    "Use the System Map's search domains; when uncertain, inspect the available directory rather than inventing a domain or filter. "
    "Tool purposes are primarily English, so use a short affirmative English task description for query. "
    "Search for the desired operation; keep excluded alternatives and all constraints in the original request "
    "for contract review rather than mixing negated alternatives into lexical query words.\n"
    "- Action conventions: list enumerates; read retrieves content; inspect checks state or metadata; "
    "set changes a setting; send delivers content; create/upsert creates or creates/updates; "
    "update/edit modifies; delete/remove/forget removes. Actual contracts determine the operation. "
    "attach connects or loads; detach disconnects or unloads; reload replaces loaded code/instances "
    "(plugin reattach is exposed as reload_plugin); restart restarts the named runtime/connection; "
    "rescan discovers metadata. These lifecycle actions are not interchangeable.\n"
    "- For a stuck or disconnected named channel endpoint, search domain='channel', query='restart endpoint'; "
    "restart_channel_endpoint rebuilds that connection. reload_plugin replaces an entire plugin/provider generation "
    "and may affect multiple endpoints; use domain='plugin' when code or provider-wide resources must be replaced. "
    "A channel endpoint name is not a plugin name.\n"
    "- Within an explicit domain, search ranks purpose and tool-authored auxiliary terms/intents without action/object gates or boosts. "
    "Inspect unmatched_terms and verify the contract against the whole request; unmatched words do not establish supported features. "
    "On a lexical miss it tries spelling recovery. Without domain, alias and conservative keyword matches take priority; "
    "when there are no hits, purpose search across filtered visible tools requires at least two positive term matches. It may return "
    "separate suggestions without changing the selected domain or filters. Suggestions do not establish capability: "
    "review each suggestion's included contract against the original request, including unmatched words. Invoke directly when it supports the task; use read_tool only for missing contract information. On a miss, directory.tools "
    "lists visible aliases and purposes in the chosen domain, independently of query relevance. Read a plausible "
    "contract before selecting it. Follow directory.next_offset with directory_offset to browse more; "
    "choose available_domains explicitly when the bucket is uncertain. An empty result is not proof "
    "that the capability is unavailable.\n"
    + TOOL_DISCOVERY_STOP_RULE +
    "- On failure or uncertain outcome, prefer result-specific recovery affordances before "
    "improvising. Respect effect, idempotency, retry, and reconcile semantics; never blindly "
    "retry a mutation.\n"
    "- Treat each tool call as one RPC. If it times out, crashes, or does not complete, its "
    "result is unavailable and its side effects may be uncertain. Inspect current state, then "
    "retry when appropriate; never infer success from the missing result.\n"
    "- Tool outputs describe state when the call ran. "
    "Replaying a stored result does not refresh mutable state. Refresh a read/status observation "
    "when it is stale, concurrent changes matter, or the outcome is uncertain; use version/ETag "
    "or conditional mutations when available. A second check is not mandatory after every call.\n"
    "Tool result metadata maps to actions:\n"
    "- kind=rejected means the requested action was refused before it started; kind=failed means the tool "
    "could not report success, so check effect to learn whether changes occurred; "
    "kind=complete means the handler returned, but check any item failures and recovery text.\n"
    "- effect describes changes, independently of success or failure: effect=none means no side effect; "
    "effect=not_started means the requested action did not start; effect=not_applied means the intended "
    "change was not made. On failure, fix the reported problem before retrying.\n"
    "- effect=applied means the action was applied, possibly only for some requested items; a later error "
    "does not undo applied work. Do not repeat completed items. effect=unknown means the harness cannot "
    "confirm what changed; inspect current state before repeating a mutation.\n"
    "- retry=correct_input means change arguments or satisfy the stated precondition; retry=safe permits "
    "a retry when useful, without promising success; retry=reconcile_first means inspect state first; "
    "retry=do_not_retry means do not repeat the call.\n"
    "- error_code identifies the failure category; the error text explains what happened. recovery gives "
    "result-specific guidance; affordances suggest next tool calls, not actions already performed or "
    "new authorization. Use them only within the authorized task.\n"
    "- Read the actual cause and any per-item failures, even when kind=complete. If output is shortened, "
    "use the returned snapshot to read the full error; an incomplete preview is not complete evidence. "
    "An output-delivery failure does not prove the original action failed or had no effect.\n"
)


TOOL_DISCOVERY_DEVELOPER_GUIDANCE = (
    "- Invoke tools with a complete native schema in the current window directly by name; the runtime handles their internal routing. Invoke other known indirect aliases through call_tool(name=alias, args=...). "
    "Search with a short affirmative English task description and a known owner domain, e.g. domain='browser', "
    "query='capture a screenshot of the current page'. When query words are uncertain, "
    "search_tools(domain=owner) browses up to 16 names and brief purposes. Read a plausible directory entry's full contract; "
    "the directory is navigation, not evidence of support. Follow next_offset for more. Include a known domain; "
    "spaces and underscores both work, word order is flexible, and a complete alias need not be guessed. Search hits include guidance and input contracts: "
    "call immediately when these suffice. Use read_tool only for missing or changed contract information, "
    "or when a validation error does not provide enough information to correct the call. "
    "Suggestions include full contracts from backend lexical retrieval: compare those contracts and unmatched words with the original request before selecting. Invoke directly when suitable; do not read the same complete contract again. "
    "On a miss, compare directory.tools with the original task, then read a plausible contract. "
    "These directory entries are navigation, not matches or proof of support. Browse more with "
    "directory_offset=directory.next_offset and the same domain/filters. Without a directory, choose "
    "available_domains and directory_offset=0. An empty search means no match for those words and filters; "
    "it does not establish that a tool is unavailable.\n"
    + TOOL_DISCOVERY_STOP_RULE
)

TOOL_RESULT_DEVELOPER_GUIDANCE = (
    "- Use each tool's guidance and returned affordances to decide the next action. "
    "After a tool call, follow a suggested next tool only when its stated `use_when` condition "
    "matches the observed result and current task.\n"
    "- Local file tools already enforce digest-based "
    "read-before-edit and compare-and-swap checks, so do not reread unchanged files merely "
    "because another conversational turn began.\n"
    "- Visual or layout conclusions need relevant rendered evidence; source text alone does not "
    "establish rendered appearance. Choose verification appropriate to the change and the user's scope. "
    "Use screenshots only when the model or a reviewer can inspect pixels."
)

TOOL_ROUTING_DEVELOPER_GUIDANCE = TOOL_DISCOVERY_DEVELOPER_GUIDANCE + TOOL_RESULT_DEVELOPER_GUIDANCE


def routing_guidance_for_tools(aliases) -> str:
    """Describe only the entrypoints exposed in this model's tool window."""
    names = set(aliases)
    if {"search_tools", "read_tool", "call_tool"} <= names:
        return TOOL_ROUTING_DEVELOPER_GUIDANCE
    routing = "Invoke exposed tools directly by name. "
    if "call_tool" in names:
        routing += "Invoke known indirect aliases through call_tool(name=alias, args=...). "
    if "read_tool" in names:
        routing += "Use read_tool only when a known tool's contract is missing or insufficient. "
    if "search_tools" in names:
        routing += "Discover tools with a known owner domain and a task description. " + TOOL_DISCOVERY_STOP_RULE
    return routing + "\n" + TOOL_RESULT_DEVELOPER_GUIDANCE


TOOL_EFFICIENCY_DEVELOPER_GUIDANCE = (
    "- Batch independent tool calls in one response, including independent reads, searches, "
    "checks, and already-decided edits to distinct surfaces. Sequence only when a later call's "
    "arguments, authority, safety, or correctness depend on an earlier result; do not serialize "
    "every file or field into its own model round. Never parallelize operations whose ordering "
    "or side effects depend on each other.\n"
    "- For code investigation, use run_shell with rg -n to locate relevant definitions, then read_file "
    "with ranges around the hits. Stop searching once the evidence supports the next action.\n"
    "- If read_file reports unchanged content, reuse the earlier result in this conversation. "
    "Reuse passing checks while the relevant code and environment remain unchanged.\n"
    "- If search_tools finds no useful capability, try a reasonable change of strategy: inspect the relevant "
    "directory, rephrase the purpose or correct guessed filters. Read a plausible contract only if information "
    "is missing. If these checks still reveal no suitable callable, it may be unavailable; explain the checked "
    "scope and uncertainty rather than continuing to search indefinitely. Reuse unchanged inventory and contracts. "
    "Use an alternative such as run_shell only when it actually satisfies the original task and is authorized; "
    "a request for a dedicated tool must not be replaced with an unrelated operation."
)

# Compatibility aliases for external prompt providers. New Pal-owned prompt
# providers must use the authority-specific constants above.
TOOL_ROUTING_SYSTEM_GUIDANCE = (
    TOOL_EXECUTION_SYSTEM_POLICY + TOOL_ROUTING_DEVELOPER_GUIDANCE
)
TOOL_EFFICIENCY_SYSTEM_GUIDANCE = TOOL_EFFICIENCY_DEVELOPER_GUIDANCE

__all__ = [
    "routing_guidance_for_tools",
    "TOOL_EFFICIENCY_DEVELOPER_GUIDANCE",
    "TOOL_DISCOVERY_STOP_RULE",
    "TOOL_EFFICIENCY_SYSTEM_GUIDANCE",
    "TOOL_EXECUTION_SYSTEM_POLICY",
    "TOOL_ROUTING_DEVELOPER_GUIDANCE",
    "TOOL_ROUTING_SYSTEM_GUIDANCE",
]

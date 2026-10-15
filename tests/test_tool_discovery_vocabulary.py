"""Search regressions against the complete authored discovery catalog.

Use real guidance and input schemas, without activating providers or invoking
mutations. Test conventional task words across domains and operation boundaries.
"""
from types import SimpleNamespace

import pytest

from scripts.audit_tool_discovery import declared_tools


@pytest.fixture(scope="module")
def catalog():
    return declared_tools()


@pytest.fixture(scope="module")
def generation(catalog):
    from pal.execution.discovery_terms import tool_domain
    return SimpleNamespace(search_records={
        tool.alias: {"alias": tool.alias, "purpose": tool.guidance.purpose,
                     "domain": tool_domain(tool.alias, tool.module_id, declared_domain=tool.guidance.search_domain), "module_id": tool.module_id,
                     "search_terms": tool.vocabulary,
                     "search_objects": tool.guidance.search_objects,
                     "search_intents": tool.guidance.search_intents,
                     "search_domain_aliases": tool.guidance.search_domain_aliases,
                     "search_domain_description": tool.guidance.search_domain_description,
                     "search_term_rules": [rule.model_dump() for rule in tool.guidance.search_term_rules],
                     "search_scope_terms": tool.guidance.search_scope_terms}
        for tool in catalog if not tool.role_scoped
    })


def aliases(generation, query, top_k=None, *, keyword_only=False):
    from pal.execution.runtime import ExecutionRuntime

    args = {"query": query}
    if top_k is not None:
        args["top_k"] = top_k
    search = ExecutionRuntime._search_keywords if keyword_only else ExecutionRuntime._search_generation
    return [hit["alias"] for hit in search(generation, args)["hits"]]


def test_catalog_preserves_every_exact_alias_and_provider_declaration(catalog, generation):
    assert any(tool.alias == "submit_candidate" and tool.role_scoped for tool in catalog)
    assert "submit_candidate" not in generation.search_records
    retired = {"attach_memory_provider", "detach_memory_provider", "set_active_memory_provider",
               "refresh_memory_provider_indexes", "list_memory_providers", "inspect_memory_provider_inventory"}
    assert not retired.intersection(tool.alias for tool in catalog)
    for tool in catalog:
        # Preserve each provider's declaration instead of relying on the union
        # of identically named tools from mutually exclusive memory backends.
        individual = SimpleNamespace(search_records={tool.alias: {
            "alias": tool.alias, "search_terms": tool.vocabulary,
        }})
        assert aliases(individual, tool.alias) == [tool.alias], tool.source
        if not tool.role_scoped:
            assert aliases(generation, tool.alias)[0] == tool.alias


@pytest.mark.parametrize("query,expected", [
    ("core status", "observe_core"),
    ("inspect core state", "observe_core"),
    ("execution status", "inspect_execution_state"),
    ("execution tool count", "inspect_execution_state"),
    ("failure errors", "inspect_failure"),
    ("list error reports", "inspect_failure"),
    ("artifact status", "inspect_artifact_state"),
    ("memory status", "inspect_memory_state"),
    ("memory provider status", "inspect_memory_state"),
    ("skill status", "inspect_skill_state"),
    ("mcp status", "inspect_mcp_state"),
    ("checklist progress", "read_checklist"),
    ("web search status", "inspect_web_search"),
    ("browser health", "inspect_browser_status"),
    ("lsp health", "inspect_lsp_status"),
    ("plugin status", "inspect_plugin"),
    ("package installation state", "inspect_package_status"),
    ("current memory provider", "inspect_memory_state"),
    ("current web search provider", "inspect_web_search"),
    ("tool schema", "read_tool"),
    ("channel notify text", "send_channel_message"),
    ("channel connect endpoint", "manage_channel_endpoint"),
    ("channel disconnect endpoint", "manage_channel_endpoint"),
    ("channel endpoint status", "inspect_channel_endpoint"),
    ("channel authentication status", "inspect_channel_endpoint"),
    ("channel pending messages", "inspect_channel_endpoint"),
    ("channel connectivity check", "inspect_channel_endpoint"),
    ("llm reasoning effort", "inspect_llm"),
    ("llm cost", "inspect_llm"),
    ("llm cache usage", "inspect_llm"),
    ("read local source file", "read_file"),
    ("patch code file", "edit_file"),
    ("create text file", "write_file"),
    ("remove folder", "run_shell"),
    ("execute shell command", "run_shell"),
    ("search repository code", "run_shell"),
    ("list directory", "run_shell"),
    ("find local files", "run_shell"),
    ("tree directory listing", "run_shell"),
    ("save memory", "remember_memory"),
    ("search memories", "recall_memory"),
    ("retrieve memory", "recall_memory"),
    ("edit memory", "update_memory"),
    ("delete memory", "forget_memory"),
    ("deduplicate memory", "manage_memory_dreaming"),
    ("create checklist", "upsert_checklist"),
    ("update checklist", "upsert_checklist"),
    ("show checklist plan", "read_checklist"),
    ("mark checklist step done", "complete_checklist_step"),
    ("import skill manual", "prepare_skill_candidate"),
    ("save skill candidate", "commit_skill_candidate"),
    ("edit skill manual", "update_skill"),
    ("find skill manuals", "search_skills"),
    ("read skill manual", "read_skill"),
    ("load skill manual", "inject_skill"),
    ("disable skill", "update_skill"),
    ("reactivate skill", "update_skill"),
    ("repair package dependencies", "prepare_package"),
    ("load plugin", "manage_plugin"),
    ("unload plugin", "manage_plugin"),
    ("restart plugin code", "manage_plugin"),
    ("discover plugins", "rescan_plugins"),
    ("mcp connect server", "manage_mcp_server"),
    ("mcp disconnect server", "manage_mcp_server"),
    ("mcp inspect server metadata", "read_mcp_server"),
    ("list scheduled tasks", "list_proactive_tasks"),
    ("proactive create task", "upsert_proactive_task"),
    ("create reminder", "upsert_proactive_task"),
    ("proactive edit task", "upsert_proactive_task"),
    ("pause scheduled task", "manage_proactive_task"),
    ("resume scheduled task", "manage_proactive_task"),
    ("remove scheduled task", "manage_proactive_task"),
    ("list proactive history", "list_proactive_runs"),
    ("proactive latest result", "read_proactive_task"),
    ("proactive reschedule cron", "update_proactive_schedule"),
    ("browser open url", "read_browser_page"),
    ("web fetch url", "read_browser_page"),
    ("read web page", "read_browser_page"),
    ("browser visit webpage", "read_browser_page"),
    ("browser read content", "read_browser_page"),
    ("browser accessibility tree", "capture_browser_snapshot"),
    ("browser search regex", "find_browser_text"),
    ("browser click button", "click_browser_target"),
    ("browser fill input", "fill_browser_field"),
    ("browser press keyboard key", "press_browser_key"),
    ("browser uncheck checkbox", "set_browser_checked_state"),
    ("browser inspect css", "inspect_browser_layout"),
    ("browser execute javascript", "evaluate_browser_script"),
    ("browser api traffic", "manage_browser_network_capture"),
    ("browser screenshot image", "capture_browser_screenshot"),
    ("browser dismiss alert", "handle_browser_dialog"),
    ("browser reload page", "navigate_browser_history"),
    ("browser open tab", "manage_browser_tabs"),
    ("browser list tabs", "manage_browser_tabs"),
    ("find web", "search_web"),
    ("internet search", "search_web"),
    ("web search provider health", "inspect_web_search"),
    ("lsp setup project", "prepare_lsp_workspace"),
    ("lsp check server readiness", "inspect_lsp_status"),
    ("lsp check errors", "read_lsp_diagnostics"),
    ("lsp read documentation", "read_lsp_hover"),
    ("lsp goto definition", "find_lsp_symbol_locations"),
    ("lsp find usages", "find_lsp_references"),
    ("lsp list file outline", "list_lsp_document_symbols"),
    ("import pdf artifact", "import_artifact"),
    ("attach image artifact", "import_artifact"),
    ("artifact get details", "inspect_artifact_info"),
    ("artifact inspect file paths", "inspect_artifact_info"),
    ("find artifact filename", "search_artifacts"),
    ("bunshin delegate task", "start_bunshin_workflow"),
    ("bunshin workflow status", "read_bunshin_task_status"),
    ("bunshin inspect profiles", "read_bunshin_catalog"),
    ("bunshin publish artifact", "submit_bunshin_artifact"),
    ("bunshin pause task", "control_bunshin_workflow"),
    ("bunshin resume workflow", "control_bunshin_workflow"),
    ("bunshin archive workflow", "control_bunshin_workflow"),
    ("bunshin answer clarification", "answer_bunshin_question"),
])
def test_reviewed_task_words_find_actual_tools(generation, query, expected):
    assert expected in aliases(generation, query)


@pytest.mark.parametrize("query,expected,excluded", [
    ("reload plugin", "manage_plugin", "rescan_plugins"),
    ("channel reload provider", "manage_plugin", "manage_channel_endpoint"),
    ("channel restart endpoint", "manage_channel_endpoint", "manage_plugin"),
    ("save memory", "remember_memory", "recall_memory"),
    ("lsp find references", "find_lsp_references", "find_lsp_symbol_locations"),
    ("send image", "send_channel_attachment", "send_channel_message"),
])
def test_synonyms_preserve_distinct_operations(generation, query, expected, excluded):
    found = aliases(generation, query)
    assert expected in found
    assert excluded not in found


@pytest.mark.parametrize("query", [
    "git status", "read image pixels", "mcp read image",
    "llm switch endpoint", "read file from web", "browser_re", "install_pack",
    "lsp finds reference", "lsp find symbols", "proactive execute task immediately",
])
def test_keyword_stage_keeps_unsupported_domains_operations_and_prefixes_unmatched(generation, query):
    assert aliases(generation, query, keyword_only=True) == []


@pytest.mark.parametrize('domain,query,expected', [
    ('checklist', 'clear checklist', 'close_checklist'),
    ('checklist', 'mark steps in progress', 'upsert_checklist'),
    ('memory', 'replace remembered content', 'update_memory'),
    ('skill', 'recover pending candidate', 'inspect_skill_state'),
    ('browser', 'inspect extension configuration workers', 'inspect_browser_status'),
    ('mcp', 'inspect server quarantine error', 'read_mcp_server'),
    ('bunshin', 'read human architecture review', 'read_bunshin_task_status'),
    ('llm', 'inspect active configuration model', 'inspect_llm'),
    ('llm', 'inspect connection capabilities context limit usage', 'inspect_llm'),
    ('llm', 'inspect cost status', 'inspect_llm'),
    ('core', 'inspect runtime status queue cache', 'observe_core'),
    ('artifact', 'inspect attachment policy', 'inspect_artifact_state'),
    ('artifact', 'list files', 'search_artifacts'),
    ('plugin', 'inspect host health', 'inspect_plugin'),
    ('proactive', 'inspect scheduled job run', 'read_proactive_task'),
    ('channel', 'reconnect channel connection', 'manage_channel_endpoint'),
])
def test_diagnostic_context_and_owned_object_names_recall_real_tools(generation, domain, query, expected):
    from pal.execution.runtime import ExecutionRuntime
    result = ExecutionRuntime._search_generation(generation, {'query': query, 'domain': domain})
    assert expected in [hit['alias'] for hit in result['hits']]
    assert all(hit['domain'] == domain for hit in result['hits'])


@pytest.mark.parametrize("top_k", [None, 5])
@pytest.mark.parametrize("query,allowed", [
    ("check checklist", {"read_checklist"}),
    ("check plan", {"read_checklist"}),
    ("check checklist progress", {"read_checklist"}),
    ("check checklist step", {"read_checklist"}),
    ("mark checklist step done", {"complete_checklist_step"}),
    ("finish checklist step", {"complete_checklist_step"}),
    ("browser network traffic", {"manage_browser_network_capture"}),
    ("browser api requests", {"manage_browser_network_capture"}),
    ("check browser state", {"inspect_browser_status"}),
    ("check browser status", {"inspect_browser_status"}),
    ("show browser state", {"inspect_browser_status"}),
    ("browser set checkbox", {"set_browser_checked_state"}),
    ("browser uncheck checkbox", {"set_browser_checked_state"}),
    ("bunshin restore profile override", set()),
    ("bunshin revert family override", set()),
    ("remove bunshin profile", set()),
    ("remove bunshin family", set()),
    ("clear bunshin profile", set()),
    ("clear bunshin family", set()),
    ("delete bunshin profile", set()),
    ("delete bunshin family", set()),
    ("create bunshin profile", set()),
    ("list directory", {"run_shell"}),
    ("list files", {"run_shell"}),
    ("list tool registry", set()),
    ("shell code search", {"run_shell"}),
    ("save memory", {"remember_memory"}),
    ("edit memory", {"update_memory"}),
    ("deduplicate memory", {"manage_memory_dreaming"}),
    ("create checklist", {"upsert_checklist"}),
    ("create reminder", {"upsert_proactive_task"}),
    ("pause scheduled task", {"manage_proactive_task"}),
    ("resume scheduled task", {"manage_proactive_task"}),
    ("remove scheduled task", {"manage_proactive_task"}),
    ("proactive execute task immediately", set()),
    ("send image", {"send_channel_attachment"}),
    ("channel notify text", {"send_channel_message"}),
    ("channel connect endpoint", {"manage_channel_endpoint"}),
    ("channel disconnect endpoint", {"manage_channel_endpoint"}),
    ("reload plugin", {"manage_plugin"}),
    ("load plugin", {"manage_plugin"}),
    ("unload plugin", {"manage_plugin"}),
    ("channel reload provider", {"manage_plugin"}),
    ("channel restart endpoint", {"manage_channel_endpoint"}),
    ("mcp connect server", {"manage_mcp_server"}),
    ("mcp disconnect server", {"manage_mcp_server"}),
    ("llm_status", {"inspect_llm"}),
    ("llm current endpoint information", {"inspect_llm"}),
    ("browser check checkbox", {"set_browser_checked_state"}),
    ("browser check radio", {"set_browser_checked_state"}),
    ("remove bunshin profile override", set()),
    ("clear bunshin family overrides", set()),
    ("delete bunshin family override", set()),
    ("llm reasoning effort", {"inspect_llm"}),
    ("llm cost", {"inspect_llm"}),
    ("llm switch endpoint", set()),
    ("lsp find references", {"find_lsp_references"}),
    ("lsp goto definition", {"find_lsp_symbol_locations"}),
    ("read image pixels", set()),
    ("transcribe audio", set()),
    ("browser clear cookies", {"manage_browser_session"}),
    ("browser reset cache", {"manage_browser_session"}),
    ("delete link target", set()),
    ("remove link target", set()),
    ("uninstall dependencies", set()),
    ("remove skill", set()),
    ("git status", set()),
    ("show identity persona", set()),
])
def test_keyword_precision_and_existing_matches_survive_purpose_fallback(generation, query, allowed, top_k):
    assert set(aliases(generation, query, top_k, keyword_only=True)) == allowed
    if allowed:
        assert set(aliases(generation, query, top_k)) == allowed


def test_browser_queries_do_not_restore_retired_fetch_tool(generation):
    assert 'fetch_browser_url' not in generation.search_records
    for query in ('browser fetch', 'browser open url', 'browser read current page'):
        assert 'fetch_browser_url' not in aliases(generation, query, 5)
    assert 'read_browser_page' in aliases(generation, 'browser open url')


def test_catalog_memory_scope_prefers_public_facade(generation):
    assert aliases(generation, 'delete memory') == ['forget_memory']
    assert aliases(generation, 'delete memory', top_k=5) == ['forget_memory']
    assert 'delete_provider_memory' not in generation.search_records


def test_backend_and_schedule_queries_recall_the_specific_owner_operation(catalog):
    from pal.execution.runtime import ExecutionRuntime
    from pal.execution.discovery_terms import tool_domain
    records = {tool.alias: {"alias": tool.alias, "domain": tool_domain(tool.alias, tool.module_id, declared_domain=tool.guidance.search_domain),
               "purpose": tool.guidance.purpose, "search_terms": tool.vocabulary,
               "search_objects": tool.guidance.search_objects,
               "search_intents": tool.guidance.search_intents,
               "search_domain_aliases": tool.guidance.search_domain_aliases,
               "search_domain_description": tool.guidance.search_domain_description,
               "search_term_rules": [rule.model_dump() for rule in tool.guidance.search_term_rules],
               "search_scope_terms": tool.guidance.search_scope_terms}
               for tool in catalog if not tool.role_scoped and tool.source != "src/pal/plugins/l3/stubs.py"}
    generation = SimpleNamespace(search_records=records)
    for domain, query, expected in (
        ("proactive", "update schedule cron task", "update_proactive_schedule"),
    ):
        hits = ExecutionRuntime._search_generation(generation, {"domain": domain, "query": query})["hits"]
        assert expected in [hit["alias"] for hit in hits]
        assert all(hit["domain"] == domain for hit in hits)


def test_open_and_read_navigation_is_scoped_to_webpage_or_url_words(generation):
    assert "read_browser_page" in aliases(generation, "browser read webpage rendered text metadata links")
    assert "read_browser_page" in aliases(generation, "browser read current page")

from __future__ import annotations

import unittest

from pal.execution.tool_search import ToolSearchTool


class ToolSearchTests(unittest.TestCase):
    def test_search_deduplicates_compact_hits_by_visible_name(self) -> None:
        class FakeRuntime:
            def list_capability_specs(self):
                return [
                    {
                        "name": "intro_module_demo_show",
                        "canonical_path": "intro_module_demo_show",
                        "description": "Show demo public metadata",
                        "family": "introspection",
                        "module_id": "demo",
                        "aliases": [],
                        "call_names": ["intro_module_demo_show"],
                        "target_id": f"endpoint_{index}",
                    }
                    for index in range(3)
                ]

        result = ToolSearchTool(FakeRuntime()).invoke({"query": "demo public", "top_k": 10})

        self.assertEqual(result.structured["total_count"], 1)
        self.assertEqual(result.structured["returned_count"], 1)
        self.assertFalse(result.structured["truncated"])
        hit = result.structured["hits"][0]
        self.assertEqual(hit["name"], "intro_module_demo_show")
        self.assertEqual(hit["description"], "Show demo public metadata")
        self.assertEqual(hit["required_params"], [])
        self.assertNotIn("target_ids", hit)
        self.assertNotIn("canonical_path", hit)
        self.assertNotIn("module_id", hit)
        self.assertNotIn("call_names", hit)

    def test_empty_search_returns_top_hits_without_facets_by_default(self) -> None:
        class FakeRuntime:
            def list_capability_specs(self):
                return [
                    {
                        "name": f"op_demo_{index}",
                        "canonical_path": f"op_demo_{index}",
                        "description": f"Demo capability {index}",
                        "family": "operation",
                        "module_id": "demo" if index < 20 else "other",
                        "aliases": [],
                        "call_names": [f"op_demo_{index}"],
                    }
                    for index in range(30)
                ]

        result = ToolSearchTool(FakeRuntime()).invoke({"top_k": 10})

        self.assertEqual(len(result.structured["hits"]), 10)
        self.assertEqual(result.structured["total_count"], 30)
        self.assertEqual(result.structured["returned_count"], 10)
        self.assertTrue(result.structured["truncated"])
        self.assertEqual(result.structured["applied_filters"], {})
        self.assertNotIn("facets", result.structured)

    def test_search_returns_deduped_facets_when_requested(self) -> None:
        class FakeRuntime:
            def list_capability_specs(self):
                return [
                    {
                        "name": "intro_module_demo_show",
                        "canonical_path": "intro_module_demo_show",
                        "description": "Show demo public metadata",
                        "family": "introspection",
                        "module_id": "demo",
                        "aliases": [],
                        "call_names": ["intro_module_demo_show"],
                        "target_id": f"endpoint_{index}",
                    }
                    for index in range(3)
                ]

        result = ToolSearchTool(FakeRuntime()).invoke({"query": "demo public", "top_k": 10, "facets": True})

        self.assertEqual(result.structured["total_count"], 1)
        self.assertEqual(result.structured["returned_count"], 1)
        self.assertIn({"module_id": "demo", "count": 1}, result.structured["facets"]["modules"])
        self.assertIn({"family": "introspection", "count": 1}, result.structured["facets"]["families"])
        self.assertIn({"namespace": "introspection", "count": 1}, result.structured["facets"]["namespaces"])

    def test_search_accepts_name_and_include_facets_aliases(self) -> None:
        class FakeRuntime:
            def list_capability_specs(self):
                return [
                    {
                        "name": "intro_module_demo_show",
                        "canonical_path": "intro_module_demo_show",
                        "description": "Show demo public metadata",
                        "family": "introspection",
                        "module_id": "demo",
                        "aliases": [],
                        "call_names": ["intro_module_demo_show"],
                    }
                ]

        result = ToolSearchTool(FakeRuntime()).invoke({"name": "demo public", "include_facets": True})

        self.assertEqual(result.structured["applied_filters"]["query"], "demo public")
        self.assertIn("facets", result.structured)

    def test_module_filter_lists_one_module_without_query(self) -> None:
        class FakeRuntime:
            def list_capability_specs(self):
                return [
                    {
                        "name": "op_alpha_one",
                        "canonical_path": "op_alpha_one",
                        "description": "Alpha one",
                        "family": "operation",
                        "module_id": "alpha",
                        "parameters_schema": {"type": "object", "required": ["value"], "properties": {"value": {"type": "string"}}},
                        "aliases": [],
                        "call_names": ["op_alpha_one"],
                    },
                    {
                        "name": "op_beta_one",
                        "canonical_path": "op_beta_one",
                        "description": "Beta one",
                        "family": "operation",
                        "module_id": "beta",
                        "aliases": [],
                        "call_names": ["op_beta_one"],
                    },
                ]

        result = ToolSearchTool(FakeRuntime()).invoke({"module_id": "alpha"})

        self.assertEqual(result.structured["total_count"], 1)
        self.assertEqual(result.structured["hits"][0]["name"], "op_alpha_one")
        self.assertEqual(result.structured["hits"][0]["required_params"], ["value"])
        self.assertNotIn("module_id", result.structured["hits"][0])
        self.assertNotIn("call_names", result.structured["hits"][0])

    def test_namespace_filter_separates_introspection_from_operations(self) -> None:
        class FakeRuntime:
            def list_capability_specs(self):
                return [
                    {
                        "name": "intro_module_llm_active",
                        "canonical_path": "intro_module_llm_active",
                        "description": "Show active llm endpoint metadata",
                        "family": "introspection",
                        "module_id": "llm",
                        "aliases": [],
                        "call_names": ["intro_module_llm_active"],
                    },
                    {
                        "name": "op_llm_mgmt_set_active_endpoint",
                        "canonical_path": "op_llm_mgmt_set_active_endpoint",
                        "description": "Switch the active llm endpoint",
                        "family": "management",
                        "module_id": "llm",
                        "aliases": [],
                        "call_names": ["op_llm_mgmt_set_active_endpoint"],
                        "metadata": {"namespace": "operation"},
                    },
                ]

        result = ToolSearchTool(FakeRuntime()).invoke(
            {"query": "llm endpoint", "namespace": "intro", "top_k": 10}
        )

        self.assertEqual([item["name"] for item in result.structured["hits"]], ["intro_module_llm_active"])
        self.assertEqual(result.structured["applied_filters"]["namespace"], "introspection")

    def test_namespace_is_inferred_from_query_terms(self) -> None:
        class FakeRuntime:
            def list_capability_specs(self):
                return [
                    {
                        "name": "intro_module_llm_active",
                        "canonical_path": "intro_module_llm_active",
                        "description": "Show active llm endpoint metadata",
                        "family": "introspection",
                        "module_id": "llm",
                        "aliases": [],
                        "call_names": ["intro_module_llm_active"],
                    },
                    {
                        "name": "op_llm_mgmt_set_active_endpoint",
                        "canonical_path": "op_llm_mgmt_set_active_endpoint",
                        "description": "Switch the active llm endpoint",
                        "family": "management",
                        "module_id": "llm",
                        "aliases": [],
                        "call_names": ["op_llm_mgmt_set_active_endpoint"],
                        "metadata": {"namespace": "operation"},
                    },
                ]

        result = ToolSearchTool(FakeRuntime()).invoke({"query": "llm endpoint config introspection", "top_k": 10})

        self.assertEqual([item["name"] for item in result.structured["hits"]], ["intro_module_llm_active"])
        self.assertEqual(result.structured["applied_filters"]["namespace"], "introspection")

    def test_management_lifecycle_and_maintenance_capabilities_are_visible_to_discovery(self) -> None:
        visible_create = {
            "name": "op_proactive_mgmt_create",
            "canonical_path": "op_proactive_mgmt_create",
            "description": "Create a proactive task",
            "family": "management",
            "module_id": "proactive",
            "aliases": [],
            "call_names": ["op_proactive_mgmt_create"],
            "metadata": {"namespace": "operation", "action": "create"},
        }
        visible_enable = {
            "name": "op_proactive_mgmt_enable",
            "canonical_path": "op_proactive_mgmt_enable",
            "description": "Enable a proactive task",
            "family": "management",
            "module_id": "proactive",
            "aliases": [],
            "call_names": ["op_proactive_mgmt_enable"],
            "metadata": {"namespace": "operation", "action": "enable"},
        }
        llm_switch = {
            "name": "op_llm_mgmt_set_active_endpoint",
            "canonical_path": "op_llm_mgmt_set_active_endpoint",
            "description": "Switch the active llm endpoint",
            "family": "management",
            "module_id": "llm",
            "aliases": [],
            "call_names": ["op_llm_mgmt_set_active_endpoint"],
            "metadata": {"namespace": "operation", "action": "set_active_endpoint"},
        }
        additional_specs = [
            {
                "name": "op_web_search_mgmt_set_config",
                "canonical_path": "op_web_search_mgmt_set_config",
                "description": "Set web search provider config",
                "family": "management",
                "module_id": "web_search",
                "aliases": [],
                "call_names": ["op_web_search_mgmt_set_config"],
                "metadata": {"namespace": "operation", "action": "set_config"},
            },
            {
                "name": "op_demo_lifecycle_attach",
                "canonical_path": "op_demo_lifecycle_attach",
                "description": "Attach demo module",
                "family": "lifecycle",
                "module_id": "demo",
                "aliases": [],
                "call_names": ["op_demo_lifecycle_attach"],
                "metadata": {"namespace": "operation", "action": "attach"},
            },
            {
                "name": "op_demo_maintenance_refresh",
                "canonical_path": "op_demo_maintenance_refresh",
                "description": "Refresh demo indexes",
                "family": "maintenance",
                "module_id": "demo",
                "aliases": [],
                "call_names": ["op_demo_maintenance_refresh"],
                "metadata": {"namespace": "operation", "action": "refresh"},
            },
        ]

        class FakeRuntime:
            def list_capability_specs(self):
                return [visible_create, visible_enable, llm_switch, *additional_specs]

        result = ToolSearchTool(FakeRuntime()).invoke({"top_k": 10})

        self.assertEqual(
            [item["name"] for item in result.structured["hits"]],
            [
                "op_proactive_mgmt_create",
                "op_proactive_mgmt_enable",
                "op_llm_mgmt_set_active_endpoint",
                "op_web_search_mgmt_set_config",
                "op_demo_lifecycle_attach",
                "op_demo_maintenance_refresh",
            ],
        )


if __name__ == "__main__":
    unittest.main()

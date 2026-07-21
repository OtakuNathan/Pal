from __future__ import annotations

import unittest

from pal.core.runtime import PalCore
from pal.execution.capabilities import register_with_core as register_execution_with_core
from pal.execution.tool_facade import (
    EffectKind,
    EmptyToolInput,
    EmptyToolOutput,
    Idempotency,
    InvocationMode,
    PagingMode,
    RetryPolicy,
    Tool,
    ToolExecutionSemantics,
    ToolGuidance,
)
from pal.llm.contracts import CanonicalToolCall


def search_fixture_tool(
    *,
    alias: str,
    text: str,
    mode: InvocationMode,
    family: str,
    module_id: str,
    tags: tuple[str, ...],
) -> Tool:
    return Tool(
        alias=alias,
        canonical_path=f"op_test_{alias}",
        InputModel=EmptyToolInput,
        OutputModel=EmptyToolOutput,
        guidance=ToolGuidance(
            purpose=text,
            use_when=text,
            do_not_use_when="another test tool is a closer match",
            failure_next_steps="search again with a narrower filter",
        ),
        execution=ToolExecutionSemantics(
            invocation_mode=mode,
            effect_kind=EffectKind.NONE,
            idempotency=Idempotency.IDEMPOTENT,
            retry_policy=RetryPolicy.AUTOMATIC,
            paging=PagingMode.NEVER,
        ),
        search_text=text,
        handler=lambda _value: {},
        family=family,
        module_id=module_id,
        metadata={"namespace": "operation", "tags": tags},
    )


class ToolSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.core = PalCore()
        register_execution_with_core(self.core.context)
        self.core.publish_module_capabilities("execution")
        runtime = self.core.context.execution_runtime
        runtime.register_tool(
            search_fixture_tool(
                alias="web_lookup",
                text="search public web pages internet research 网页搜索",
                mode=InvocationMode.DIRECT,
                family="web",
                module_id="web_search",
                tags=("network", "research"),
            )
        )
        runtime.register_tool(
            search_fixture_tool(
                alias="memory_lookup",
                text="recall durable memory facts and prior cases 记忆召回",
                mode=InvocationMode.INDIRECT,
                family="memory",
                module_id="memory",
                tags=("recall", "durable"),
            )
        )

    def tearDown(self) -> None:
        self.core.context.execution_runtime.shutdown()

    def search(self, **args: object) -> dict[str, object]:
        result = self.core.context.execution_runtime.execute_tool(
            CanonicalToolCall(name="search_tools", args=dict(args))
        )
        self.assertTrue(result.ok, result.text)
        return dict(result.structured or {})

    def test_exact_alias_ranks_first_and_returns_compact_contract(self) -> None:
        payload = self.search(query="web_lookup", top_k=10)
        hit = payload["hits"][0]
        self.assertEqual(hit["alias"], "web_lookup")
        self.assertGreaterEqual(hit["score"], 100)
        self.assertEqual(hit["invocation_mode"], "direct")
        self.assertIn("input_shape", hit)
        self.assertNotIn("description", hit)

    def test_filters_apply_to_generation_metadata(self) -> None:
        payload = self.search(
            namespace="action",
            module_id="memory",
            family="memory",
            tags=["durable"],
        )
        self.assertEqual([hit["alias"] for hit in payload["hits"]], ["memory_lookup"])
        self.assertEqual(payload["applied_filters"]["namespace"], "operation")

    def test_facets_count_filtered_candidates(self) -> None:
        payload = self.search(query="lookup", top_k=1, facets=True)
        self.assertTrue(payload["truncated"])
        self.assertIn("facets", payload)
        modules = {item["module_id"]: item["count"] for item in payload["facets"]["modules"]}
        self.assertEqual(modules["memory"], 1)
        self.assertEqual(modules["web_search"], 1)

    def test_jieba_terms_find_chinese_search_text(self) -> None:
        payload = self.search(query="记忆召回")
        self.assertEqual(payload["hits"][0]["alias"], "memory_lookup")


if __name__ == "__main__":
    unittest.main()

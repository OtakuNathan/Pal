from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from pal.execution.contracts import CapabilityResult
from pal.execution.generated_tool_models import WebSearchCapabilitiesWebSearchIntrospectionProviderQueryInput
from pal.web_fetch.tool_models import BrowserReadInput
from pal.execution.tool_result_pager import ToolResultPagerStore
from pal.bunshin.ipc import ROLE_GATEWAY_TOKEN_ENV
from pal.bunshin.manager import BunshinManager, BunshinRunState
from pal.bunshin.web_broker import BunshinBrokerWebClient
from pal.shared import IntrospectionCall, IntrospectionResult, BunshinInvocationPack
from pal.web_fetch.capabilities import WebFetchIntrospectionProvider
from pal.web_search.capabilities import WebSearchIntrospectionProvider


class BunshinWebBrokerTests(unittest.TestCase):
    def test_role_pager_keeps_payload_in_memory_and_uses_explicit_lifetime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_bunshin_pager_") as tmp:
            root = Path(tmp)
            pager = ToolResultPagerStore()
            pager.begin_turn(
                runtime_root=root,
                turn_id="turn-1",
                scope_key="role-assignment",
            )
            handle = pager.store(
                runtime_root=root,
                turn_id="turn-1",
                result_ref="result-1",
                tool_name="browser_read",
                status="ok",
                ok=True,
                rendered="x" * 5000,
                page_size=1000,
            )

            self.assertEqual(handle.execution_lifetime_id, "role-assignment")
            self.assertFalse(hasattr(handle, "backing_path"))
            self.assertFalse((root / "data" / "tool_results").exists())
            self.assertEqual(
                pager.read_page(
                    "result-1",
                    page=2,
                    execution_lifetime_id="role-assignment",
                ).content,
                "x" * 1000,
            )

    def test_sandbox_client_requires_assignment_token_and_uses_unix_only(self) -> None:
        broker = BunshinBrokerWebClient(Path("/tmp/pal-web-broker"), "run-web")
        with patch.dict(
            os.environ,
            {
                "PAL_BUNSHIN_WEB_BROKER": "1",
                "PAL_BUNSHIN_SANDBOXED": "1",
                ROLE_GATEWAY_TOKEN_ENV: "",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "assignment-scoped"):
                _ = broker._client

        with patch.dict(
            os.environ,
            {
                "PAL_BUNSHIN_WEB_BROKER": "1",
                "PAL_BUNSHIN_SANDBOXED": "1",
                ROLE_GATEWAY_TOKEN_ENV: "assignment-token",
            },
            clear=False,
        ):
            client = broker._client

        self.assertTrue(client._client.unix_only)

    def test_web_capability_providers_delegate_without_local_network(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        def search(args: dict[str, object]) -> IntrospectionResult:
            calls.append(("search", dict(args)))
            return IntrospectionResult(
                status="ok",
                text="host search",
                llm_text="host search",
                structured={"items": [{"title": "result"}]},
            )

        def read(args: dict[str, object]) -> IntrospectionResult:
            calls.append(("read", dict(args)))
            return IntrospectionResult(
                status="ok",
                text="host read",
                llm_text="host read",
                structured={"text": "page"},
            )

        search_provider = WebSearchIntrospectionProvider(
            service=SimpleNamespace(),
            query_delegate=search,
        )
        fetch_provider = WebFetchIntrospectionProvider(
            service=SimpleNamespace(),
            read_delegate=read,
        )

        search_result = search_provider.query(
            IntrospectionCall(name="op_web_search", args={"query": "framepipe"})
        )
        read_result = fetch_provider.read(
            IntrospectionCall(
                name="op_browser_read",
                args={"url": "https://example.invalid"},
            )
        )

        self.assertEqual(search_result.text, "host search")
        self.assertEqual(read_result.text, "host read")
        self.assertEqual(
            calls,
            [
                ("search", {"query": "framepipe"}),
                ("read", {"url": "https://example.invalid"}),
            ],
        )

    def test_manager_validates_assignment_scope_and_web_input_before_host_call(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory(prefix="pal_bunshin_web_broker_") as tmp:
                manager = BunshinManager(Path(tmp))
                pack = BunshinInvocationPack(
                    invocation_id="web-invocation",
                    allowed_capabilities=["op_web_search", "op_browser_read"],
                )
                manager.runs["run-web"] = BunshinRunState(
                    bunshin_id="session-web",
                    run_id="run-web",
                    pack=pack,
                )
                observed: list[object] = []

                class Runtime:
                    registry_generation = SimpleNamespace(
                        direct_aliases={
                            "search_web": SimpleNamespace(
                                canonical_path="op_web_search",
                                input_model=WebSearchCapabilitiesWebSearchIntrospectionProviderQueryInput,
                            ),
                            "browser_read": SimpleNamespace(
                                canonical_path="op_browser_read",
                                input_model=BrowserReadInput,
                            ),
                        }
                    )

                    async def call_registered_async(self, call):
                        observed.append(call)
                        return CapabilityResult(
                            status="ok",
                            text="web search results",
                            llm_text="web search results",
                            structured={"items": [{"title": "framepipe"}]},
                        )

                async def bundle():
                    return SimpleNamespace(execution_runtime=Runtime())

                manager._host_tool_runtime_bundle = bundle  # type: ignore[method-assign]
                result = await manager.web_broker_search(
                    {
                        "run_id": "run-web",
                        "args": {"query": "framepipe", "limit": 3},
                    }
                )
                self.assertEqual(
                    result["result"]["structured"]["items"][0]["title"],
                    "framepipe",
                )
                self.assertEqual(observed[0].name, "op_web_search")
                self.assertEqual(observed[0].args["limit"], 3)

                read_result = await manager.web_broker_read(
                    {
                        "run_id": "run-web",
                        "args": {"url": "https://example.com", "max_chars": 1000},
                    }
                )
                self.assertEqual(read_result["result"]["status"], "ok")
                self.assertEqual(observed[1].name, "op_browser_read")
                self.assertEqual(observed[1].meta["broker_run_id"], "run-web")

                with self.assertRaises(ValidationError):
                    await manager.web_broker_search(
                        {
                            "run_id": "run-web",
                            "args": {"query": "framepipe", "unexpected": True},
                        }
                    )

                manager.runs["run-web"].pack = BunshinInvocationPack(
                    invocation_id="web-denied",
                    allowed_capabilities=[],
                )
                with self.assertRaises(PermissionError):
                    await manager.web_broker_search(
                        {
                            "run_id": "run-web",
                            "args": {"query": "framepipe"},
                        }
                    )

        asyncio.run(scenario())

    def test_role_gateway_authentication_wraps_web_broker_methods(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory(prefix="pal_bunshin_web_auth_") as tmp:
                manager = BunshinManager(Path(tmp))
                manager.runs["run-web"] = BunshinRunState(
                    bunshin_id="session-web",
                    run_id="run-web",
                    pack=BunshinInvocationPack(
                        invocation_id="web-auth",
                        allowed_capabilities=["op_web_search"],
                    ),
                )
                manager.role_gateway.authorize = lambda token: {
                    "assignment": {"session_id": "session-web"},
                    "token": token,
                }

                async def search(payload):
                    self.assertNotIn("access_token", payload)
                    return {"ok": True}

                manager.web_broker_search = search  # type: ignore[method-assign]
                result = await manager._call_worker_method(
                    "web_search",
                    {
                        "access_token": "assignment-token",
                        "run_id": "run-web",
                        "args": {"query": "framepipe"},
                    },
                )
                self.assertTrue(result["ok"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

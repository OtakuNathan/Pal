"""H-group acceptance: static relations stay in descriptors, and successful
results emit zero capability reminders — first call, changed workspace, or
after history changes alike (task package v2 §7).

Sequence under test: A ready -> B ready -> B partial -> A partial. The first
two results carry no guidance; the last two carry recovery bound to the
failing workspace; the descriptor never changes.
"""
from __future__ import annotations

from pathlib import Path

from pal.core import PalCore
from pal.execution import register_with_core as register_execution_with_core
from pal.execution.contracts import CapabilityCall, ToolCallBudget
from pal.lsp import build_lsp_plugin
from pal.lsp.plugin import LspManagerPluginProvider
from pal.shared.tool_protocol import new_tool_call

READY_FORBIDDEN = ("lsp_doctor", "lsp_status", "lsp_rescan", "read_tool", "call_tool",
                   "lsp_hover", "lsp_definition", "lsp_references", "lsp_diagnostics",
                   "lsp_document_symbols", "next_tools")


def _payload_ready(root: str) -> dict:
    return {
        "status": "ok",
        "workspace_root": root,
        "primary_server": "clangd",
        "primary_probe_ready": True,
        "servers": [{"server_id": "clangd", "status": "ok"}],
    }


def _payload_partial(root: str, server: str | None = "clangd") -> dict:
    payload = {
        "status": "partial",
        "workspace_root": root,
        "primary_probe_ready": False,
        "servers": [{"server_id": "yaml", "status": "ok"}],
    }
    if server is not None:
        payload["primary_server"] = server
        payload["servers"].append({"server_id": server, "status": "init_failed"})
    return payload


class TestProviderLevel:
    """Direct provider contract: result shape per business status."""

    def _provider(self, responses: list[dict]) -> LspManagerPluginProvider:
        provider = LspManagerPluginProvider(runtime_root=Path("/tmp/pal-lsp-test-unused"))
        queue = list(responses)

        def fake_rpc(_method, params=None):
            _ = params
            if queue:
                item = queue.pop(0)
            else:
                item = responses[-1]
            return dict(item)

        provider._request_or_error = fake_rpc  # type: ignore[method-assign]
        return provider

    def _prepare(self, provider, root: str):
        return provider.prepare_workspace(
            CapabilityCall(name="lsp_prepare_workspace", args={"workspace_root": root})
        )

    def test_ready_first_call_has_no_guidance(self):
        provider = self._provider([_payload_ready("/w/A")])
        result = self._prepare(provider, "/w/A")
        assert not result.affordances
        assert result.recovery_hint == ""
        assert "next_tools" not in (result.structured or {})
        for token in READY_FORBIDDEN:
            assert token not in result.llm_text, token

    def test_second_workspace_ready_still_has_no_guidance(self):
        provider = self._provider([_payload_ready("/w/A"), _payload_ready("/w/B")])
        first = self._prepare(provider, "/w/A")
        second = self._prepare(provider, "/w/B")
        for result in (first, second):
            assert not result.affordances
            assert result.recovery_hint == ""
            assert "next_tools" not in (result.structured or {})
        # Workspace identity stays a fact (H07).
        assert second.structured["workspace_root"] == "/w/B"
        assert second.structured["primary_server"] == "clangd"

    def test_partial_with_known_server_binds_doctor_to_that_server(self):
        provider = self._provider([_payload_partial("/w/B", server="clangd")])
        result = self._prepare(provider, "/w/B")
        tools = {item.tool: item for item in result.affordances}
        assert set(tools) <= {"lsp_doctor", "lsp_status"}
        doctor = tools.get("lsp_doctor")
        if doctor is not None:
            assert doctor.arguments.get("workspace_root") == "/w/B"
            assert doctor.arguments.get("name") == "clangd"
        assert "next_tools" not in (result.structured or {})

    def test_partial_with_unknown_server_does_not_guess(self):
        provider = self._provider([_payload_partial("/w/B", server=None)])
        result = self._prepare(provider, "/w/B")
        for item in result.affordances:
            assert "name" not in item.arguments
            assert item.arguments.get("workspace_root") == "/w/B"
            assert item.tool in {"lsp_status"}

    def test_partial_then_ready_stops_suggesting(self):
        provider = self._provider(
            [_payload_partial("/w/A", server="clangd"), _payload_ready("/w/A")]
        )
        partial = self._prepare(provider, "/w/A")
        ready = self._prepare(provider, "/w/A")
        assert partial.affordances != [] or partial.recovery_hint != ""
        assert not ready.affordances
        assert ready.recovery_hint == ""
        for token in READY_FORBIDDEN:
            assert token not in ready.llm_text, token

    def test_independent_partials_keep_their_own_recovery(self):
        provider = self._provider(
            [
                _payload_partial("/w/A", server="clangd"),
                _payload_partial("/w/B", server="pyright"),
            ]
        )
        first = self._prepare(provider, "/w/A")
        second = self._prepare(provider, "/w/B")
        first_binding = {
            item.arguments.get("workspace_root") for item in first.affordances
        }
        second_binding = {
            item.arguments.get("workspace_root") for item in second.affordances
        }
        assert first_binding == {"/w/A"}
        assert second_binding == {"/w/B"}
        second_servers = {
            item.arguments.get("name") for item in second.affordances if "name" in item.arguments
        }
        assert second_servers == {"pyright"}

    def test_failed_prepare_keeps_error_with_bound_recovery(self):
        payload = _payload_partial("/w/A", server="clangd")
        payload["status"] = "unavailable"
        payload["error"] = "no language server matched"
        provider = self._provider([payload])
        result = self._prepare(provider, "/w/A")
        assert "no language server matched" in result.llm_text
        assert result.affordances, "failed preparation keeps a bound recovery entry"

    def test_ready_facts_do_not_become_reasons(self):
        # Same payload shape with a new run id/path/server: still zero
        # guidance, proving identity fields alone are not triggers (H07/H19).
        provider = self._provider([_payload_ready("/w/C")])
        result = self._prepare(provider, "/w/C")
        assert not result.affordances
        assert result.recovery_hint == ""

    def test_diagnostics_empty_and_ready_has_no_doctor(self):
        provider = self._provider([{"status": "ok", "diagnostics": []}])
        result = provider.diagnostics(
            CapabilityCall(name="lsp_diagnostics", args={"file": "/w/A/src.py"})
        )
        assert not result.affordances
        assert result.recovery_hint == ""

    def test_call_hierarchy_single_item_binds_position_continuation(self):
        payload = {
            "status": "ok",
            "items": [{"name": "handle", "kind": "function"}],
        }
        provider = self._provider([payload])
        result = provider.prepare_call_hierarchy(
            CapabilityCall(
                name="lsp_prepare_call_hierarchy",
                args={"file": "/w/A/src.py", "line": 10, "character": 4},
            )
        )
        tools = sorted(item.tool for item in result.affordances)
        assert tools == ["lsp_incoming_calls", "lsp_outgoing_calls"]
        for item in result.affordances:
            assert item.arguments["file"] == "/w/A/src.py"
            assert item.arguments["line"] == 10
            assert item.arguments["character"] == 4

    def test_call_hierarchy_empty_does_not_fabricate(self):
        provider = self._provider([{"status": "ok", "items": []}])
        result = provider.prepare_call_hierarchy(
            CapabilityCall(
                name="lsp_prepare_call_hierarchy",
                args={"file": "/w/A/src.py", "line": 10, "character": 4},
            )
        )
        assert not result.affordances
        assert result.recovery_hint == ""

    def test_call_hierarchy_multiple_items_does_not_pick_first(self):
        payload = {
            "status": "ok",
            "items": [
                {"name": "handle", "kind": "function"},
                {"name": "handle", "kind": "method", "container": "impl"},
            ],
        }
        provider = self._provider([payload])
        result = provider.prepare_call_hierarchy(
            CapabilityCall(
                name="lsp_prepare_call_hierarchy",
                args={"file": "/w/A/src.py", "line": 10, "character": 4},
            )
        )
        assert len(result.structured["items"]) == 2
        # Multiple ambiguous items: facts stay, no arbitrary continuation.
        assert not result.affordances


class TestEndToEndSequence:
    """H01-H03/H10/H13: the full exit path and the descriptor invariants."""

    def _core(self, tmp_path):
        core = PalCore()
        register_execution_with_core(core.context)
        handle = build_lsp_plugin(runtime_root=tmp_path).register_with_core(core.context)
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities("lsp")
        return core, handle

    def test_ready_ready_partial_partial_sequence(self, tmp_path):
        core, handle = self._core(tmp_path)
        provider = handle.ports["lsp"]
        responses = [
            _payload_ready("/w/A"),
            _payload_ready("/w/B"),
            _payload_partial("/w/B", server="clangd"),
            _payload_partial("/w/A", server="clangd"),
        ]
        queue = list(responses)

        def fake_rpc(_method, params=None):
            _ = params
            return dict(queue.pop(0) if queue else responses[-1])

        provider._request_or_error = fake_rpc  # type: ignore[method-assign]
        runtime = core.context.execution_runtime
        generation_hash_before = runtime.registry_generation.generation_hash
        try:
            budget = ToolCallBudget(max_output_chars=4000)
            ready_a = runtime.execute_tool(
                new_tool_call(
                    name="lsp_prepare_workspace", args={"workspace_root": "/w/A"}, call_id="h1"
                ),
                budget=budget,
                turn_id="t",
            )
            ready_b = runtime.execute_tool(
                new_tool_call(
                    name="lsp_prepare_workspace", args={"workspace_root": "/w/B"}, call_id="h2"
                ),
                budget=budget,
                turn_id="t",
            )
            for ready in (ready_a, ready_b):
                assert ready.ok
                invocation = ready.invocation_result
                assert invocation.affordances == []
                assert invocation.recovery_hint == ""
                for token in READY_FORBIDDEN:
                    assert token not in ready.llm_text, token
                assert "next_tools" not in (ready.structured or {})

            partial_b = runtime.execute_tool(
                new_tool_call(
                    name="lsp_prepare_workspace", args={"workspace_root": "/w/B"}, call_id="h3"
                ),
                budget=budget,
                turn_id="t",
            )
            partial_a = runtime.execute_tool(
                new_tool_call(
                    name="lsp_prepare_workspace", args={"workspace_root": "/w/A"}, call_id="h4"
                ),
                budget=budget,
                turn_id="t",
            )
            for partial, workspace in ((partial_b, "/w/B"), (partial_a, "/w/A")):
                assert partial.ok
                invocation = partial.invocation_result
                assert invocation.affordances, workspace
                bound = {
                    item.arguments["args"].get("workspace_root")
                    for item in invocation.affordances
                }
                assert bound == {workspace}
            # The static descriptor never changed and still advertises the
            # conditional diagnostic relation (H10).
            assert runtime.registry_generation.generation_hash == generation_hash_before
            record = runtime.registry_generation.direct_aliases["lsp_prepare_workspace"]
            assert "lsp_doctor" in record.compiled_description
            assert "Failure next steps" not in record.compiled_description
        finally:
            core.close()

    def test_read_tool_shows_relation_not_handbook(self, tmp_path):
        core, handle = self._core(tmp_path)
        runtime = core.context.execution_runtime
        try:
            read = runtime.execute_tool(
                new_tool_call(
                    name="read_tool", args={"name": "lsp_prepare_workspace"}, call_id="h11"
                ),
                turn_id="t",
            )
            assert "lsp_doctor" in read.llm_text
            assert "lsp_status" in read.llm_text
            assert "Failure next steps" not in read.llm_text
            assert "before retrying" not in read.llm_text
        finally:
            core.close()

    def test_declared_hints_are_not_enumerated_into_results(self, tmp_path):
        # H21: static hints exist on the record, but a ready result stays
        # empty — the runtime never enumerates hints into affordances.
        core, handle = self._core(tmp_path)
        provider = handle.ports["lsp"]
        provider._request_or_error = lambda _m, p=None: dict(_payload_ready("/w/A"))  # type: ignore[method-assign]
        runtime = core.context.execution_runtime
        try:
            record = runtime.registry_generation.direct_aliases["lsp_prepare_workspace"]
            assert record.guidance.next_tool_hints
            result = runtime.execute_tool(
                new_tool_call(
                    name="lsp_prepare_workspace",
                    args={"workspace_root": "/w/A"},
                    call_id="h21",
                ),
                turn_id="t",
            )
            assert result.ok
            assert result.invocation_result.affordances == []
        finally:
            core.close()

    def test_selection_ignores_display_history(self, tmp_path):
        # H20: identical partial facts produce identical guidance regardless
        # of how many times guidance was shown before.
        core, handle = self._core(tmp_path)
        provider = handle.ports["lsp"]
        provider._request_or_error = lambda _m, p=None: dict(_payload_partial("/w/A", server="clangd"))  # type: ignore[method-assign]
        runtime = core.context.execution_runtime
        try:
            seen: list[tuple] = []
            for index in range(3):
                result = runtime.execute_tool(
                    new_tool_call(
                        name="lsp_prepare_workspace",
                        args={"workspace_root": "/w/A"},
                        call_id=f"h20-{index}",
                    ),
                    turn_id="t",
                )
                invocation = result.invocation_result
                signature = (
                    tuple(sorted(item.tool for item in invocation.affordances)),
                    invocation.recovery_hint,
                )
                assert signature not in seen or seen[0] == signature
                seen.append(signature)
            assert seen[0] == seen[1] == seen[2]
        finally:
            core.close()

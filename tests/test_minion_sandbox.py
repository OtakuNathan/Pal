from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest, CanonicalToolCall, CanonicalToolResult, LLMPreflightAdvice, LLMPreflightRequest
from pal.minion.manager import MinionManager, MinionRunState
from pal.minion.llm_broker import (
    llm_outcome_from_payload,
    llm_outcome_to_payload,
    llm_request_from_payload,
    llm_request_to_payload,
    preflight_advice_from_payload,
    preflight_advice_to_payload,
    preflight_request_from_payload,
    preflight_request_to_payload,
)
from pal.minion.runner import MinionRunner, MinionRuntimeBundle
from pal.minion.sandbox import (
    build_sandboxed_runner_invocation,
    ensure_sandbox_files,
    scrub_minion_sandbox_env,
    with_minion_sandbox_metadata,
)
from pal.shared import RuntimeStatus, TaskContextPack


class MinionSandboxTests(unittest.TestCase):
    def test_sandbox_metadata_defaults_to_available_backend_or_unavailable_marker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_meta_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            pack = TaskContextPack(work_order_id="wo", goal="g", workspace={"repo_path": str(repo)})

            updated = with_minion_sandbox_metadata(root, pack, run_id="run_1")

            sandbox = updated.metadata["sandbox"]
            self.assertIn("enabled", sandbox)
            if sandbox["backend"] != "unavailable":
                self.assertTrue(sandbox["enabled"])
                self.assertEqual(sandbox["backend"], "bwrap")
                self.assertEqual(sandbox["workspace_path"], str(repo))
                self.assertEqual(sandbox["secret_policy"], "host_llm_broker")
                self.assertIn("sudo", sandbox["blacklist_commands"])
            else:
                self.assertTrue(sandbox["enabled"])
                self.assertEqual(sandbox["backend"], "unavailable")

    def test_sandbox_metadata_rejects_unwired_backend(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_backend_") as tmp:
            pack = TaskContextPack(
                work_order_id="wo",
                goal="g",
                metadata={"sandbox": {"backend": "docker"}},
            )

            updated = with_minion_sandbox_metadata(Path(tmp), pack, run_id="run_backend")

            sandbox = updated.metadata["sandbox"]
            self.assertTrue(sandbox["enabled"])
            self.assertEqual(sandbox["backend"], "unavailable")
            self.assertIn("unsupported", sandbox["reason"])

    def test_sandbox_env_scrubs_secret_like_values_and_enables_broker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_env_") as tmp:
            env = scrub_minion_sandbox_env(
                {
                    "PATH": "/usr/bin",
                    "OPENAI_API_KEY": "secret",
                    "NORMAL_VALUE": "kept",
                    "PAL_TOKEN": "secret",
                },
                runtime_root=Path(tmp),
                run_id="run_env",
            )

            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(env["NORMAL_VALUE"], "kept")
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("PAL_TOKEN", env)
            self.assertEqual(env["PAL_MINION_LLM_BROKER"], "1")
            self.assertEqual(env["PAL_MINION_SANDBOXED"], "1")
            self.assertIn("run_env", env["TMPDIR"])

    def test_blacklist_wrappers_are_generated_as_executable_route_blocks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_wrappers_") as tmp:
            scratch, deny_dir = ensure_sandbox_files(Path(tmp), run_id="run_wrap", blacklist_commands=("sudo", "docker"))

            self.assertTrue((scratch / "tmp").is_dir())
            sudo = deny_dir / "sudo"
            docker = deny_dir / "docker"
            self.assertTrue(os.access(sudo, os.X_OK))
            self.assertTrue(os.access(docker, os.X_OK))
            sudo_text = sudo.read_text(encoding="utf-8")
            self.assertIn("blocked command 'sudo'", sudo_text)
            self.assertIn("Use Pal resident capabilities when available", sudo_text)
            self.assertIn("read_file for repo file reads", sudo_text)
            self.assertIn("delete_path for deleting repo paths", sudo_text)
            self.assertIn("Keep run_shell for sandbox-local tests", sudo_text)

    def test_runner_invocation_uses_broker_env_when_sandboxed(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_invocation_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            pack = TaskContextPack(
                work_order_id="wo",
                goal="g",
                workspace={"repo_path": str(repo)},
                metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "run_inv"}},
            )

            argv, env = build_sandboxed_runner_invocation(
                runtime_root=root,
                pack=pack,
                argv=["python", "-m", "pal.minion.runner_main"],
                env={"PATH": "/usr/bin", "OPENAI_API_KEY": "secret"},
            )

            self.assertTrue(argv[0].endswith("bwrap"))
            self.assertIn("--share-net", argv)
            self.assertIn("--chdir", argv)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertEqual(env["PAL_MINION_LLM_BROKER"], "1")
            self.assertIn("PYTHONPATH", env)

    def test_sandboxed_python_can_import_runtime_dependencies(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_import_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            pack = TaskContextPack(
                work_order_id="wo",
                goal="g",
                workspace={"repo_path": str(repo)},
                metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "run_import"}},
            )

            argv, env = build_sandboxed_runner_invocation(
                runtime_root=root,
                pack=pack,
                argv=["python", "-c", "import msgpack; import pal.foundation.sidecar; print('imports-ok')"],
                env={"PATH": "/usr/bin:/bin"},
            )
            result = subprocess.run(argv, env=env, cwd=str(repo), capture_output=True, text=True, timeout=20)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("imports-ok", result.stdout)

    def test_sandboxed_runner_skips_shell_parser_and_approval(self) -> None:
        async def scenario() -> None:
            calls: list[str] = []
            pack = TaskContextPack(
                work_order_id="wo_sandbox_shell",
                goal="sandbox shell",
                allowed_capabilities=["op_exec_shell", "op_file_read"],
                approval_policy={"high_risk_capabilities": ["op_exec_shell"]},
                metadata={"sandbox": {"enabled": True, "backend": "bwrap"}},
            )

            class FakeExecution:
                async def execute_tool_async(self, call, **kwargs):
                    _ = kwargs
                    calls.append(str(call.args.get("cmd") or ""))
                    return CanonicalToolResult(
                        name=call.name,
                        ok=True,
                        text="shell ok",
                        llm_text="shell ok",
                        structured={"exit_code": 0},
                        status=RuntimeStatus.OK,
                    )

            async def write_event(event):
                _ = event

            async def read_decision(timeout):
                _ = timeout
                raise AssertionError("sandboxed shell should not request approval")

            runner = MinionRunner(
                runtime_root=Path(tempfile.mkdtemp(prefix="pal_minion_sandbox_runner_")),
                pack=pack,
                minion_id="m_sandbox_shell",
                run_id="r_sandbox_shell",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=SimpleNamespace(), execution_runtime=SimpleNamespace()),
            )
            result = await runner._execute_allowed_tool(
                FakeExecution(),
                CanonicalToolCall(name="op_exec_shell", args={"cmd": "cat README.md"}, call_id="call_shell"),
            )

            self.assertTrue(result.ok, result.text)
            self.assertEqual(calls, ["cat README.md"])

        asyncio.run(scenario())


class MinionLLMBrokerSerializationTests(unittest.TestCase):
    def test_llm_request_round_trips(self) -> None:
        request = CanonicalLLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=123,
            model_hint="model",
            temperature=0.2,
            tools=[{"type": "function", "function": {"name": "tool"}}],
            metadata={"run_id": "r"},
        )

        restored = llm_request_from_payload(llm_request_to_payload(request))

        self.assertEqual(restored.messages, request.messages)
        self.assertEqual(restored.max_output_tokens, 123)
        self.assertEqual(restored.model_hint, "model")
        self.assertEqual(restored.temperature, 0.2)
        self.assertEqual(restored.tools, request.tools)
        self.assertEqual(restored.metadata, request.metadata)

    def test_llm_outcome_round_trips_tool_calls_and_provider_fields(self) -> None:
        outcome = CanonicalLLMOutcome(
            text="ok",
            reasoning_text="hidden",
            tool_calls=[CanonicalToolCall(name="op_exec_shell", args={"cmd": "pwd"}, call_id="call_1")],
            finish_reason="tool_calls",
            provider_specific_fields={"reasoning_content": "hidden"},
        )

        restored = llm_outcome_from_payload(llm_outcome_to_payload(outcome))

        self.assertEqual(restored.text, "ok")
        self.assertEqual(restored.reasoning_text, "hidden")
        self.assertEqual(restored.finish_reason, "tool_calls")
        self.assertEqual(restored.tool_calls[0].name, "op_exec_shell")
        self.assertEqual(restored.tool_calls[0].args, {"cmd": "pwd"})
        self.assertEqual(restored.provider_specific_fields["reasoning_content"], "hidden")

    def test_preflight_round_trips(self) -> None:
        request = LLMPreflightRequest(
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=50,
            model_hint="m",
            tools=[{"name": "tool"}],
            metadata={"preferred_endpoint_id": "e"},
        )
        advice = LLMPreflightAdvice(
            status="ready",
            active_model="m",
            fallback_chain=["f"],
            target_input_budget=10,
            reserved_output_tokens=5,
            breakdown={"ok": True},
        )

        restored_request = preflight_request_from_payload(preflight_request_to_payload(request))
        restored_advice = preflight_advice_from_payload(preflight_advice_to_payload(advice))

        self.assertEqual(restored_request.messages, request.messages)
        self.assertEqual(restored_request.tools, request.tools)
        self.assertEqual(restored_advice.status, "ready")
        self.assertEqual(restored_advice.active_model, "m")
        self.assertEqual(restored_advice.fallback_chain, ["f"])

    def test_manager_llm_broker_calls_host_runtime(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory(prefix="pal_minion_broker_manager_") as tmp:
                manager = MinionManager(runtime_root=Path(tmp))
                pack = TaskContextPack(work_order_id="wo_broker", goal="g")
                manager.runs["run_broker"] = MinionRunState(minion_id="m", run_id="run_broker", pack=pack, status="running")

                class FakeRuntime:
                    async def apreflight(self, request):
                        self.preflight_request = request
                        return LLMPreflightAdvice(status="ready", active_model="fake")

                    async def agenerate(self, request):
                        self.generate_request = request
                        return CanonicalLLMOutcome(text="pong", finish_reason="stop")

                    def resolve_max_output_tokens(self, **kwargs):
                        self.max_kwargs = kwargs
                        return 123

                    def resolve_endpoint_facts(self, **kwargs):
                        self.facts_kwargs = kwargs
                        return {"endpoint_id": kwargs.get("preferred_endpoint_id"), "model_id": "fake"}

                fake = FakeRuntime()

                async def fake_runtime():
                    return fake

                manager._llm_broker_runtime = fake_runtime  # type: ignore[method-assign]
                preflight = await manager.llm_broker_preflight(
                    {
                        "run_id": "run_broker",
                        "request": preflight_request_to_payload(
                            LLMPreflightRequest(messages=[{"role": "user", "content": "ping"}], max_output_tokens=10)
                        ),
                    }
                )
                generated = await manager.llm_broker_generate(
                    {
                        "run_id": "run_broker",
                        "request": llm_request_to_payload(
                            CanonicalLLMRequest(messages=[{"role": "user", "content": "ping"}], max_output_tokens=10)
                        ),
                    }
                )
                max_tokens = await manager.llm_broker_resolve_max_output_tokens(
                    {"run_id": "run_broker", "preferred_endpoint_id": "endpoint_a"}
                )
                facts = await manager.llm_broker_resolve_endpoint_facts({"run_id": "run_broker", "preferred_endpoint_id": "endpoint_a"})

                self.assertEqual(preflight["advice"]["active_model"], "fake")
                self.assertEqual(generated["outcome"]["text"], "pong")
                self.assertEqual(max_tokens["max_output_tokens"], 123)
                self.assertEqual(facts["endpoint_id"], "endpoint_a")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

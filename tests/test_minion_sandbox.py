from __future__ import annotations

import asyncio
import io
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import msgpack

from pal.execution.git_tool import classify_git_command
from pal.llm import EndpointResolver, LLMRuntime
from pal.lsp.ipc import LspManagerClient
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest, CanonicalToolCall, CanonicalToolResult, LLMPreflightAdvice, LLMPreflightRequest
from pal.minion.manager import MinionManager, MinionRunState
from pal.minion.ipc import ROLE_GATEWAY_TOKEN_ENV, MinionRoleGatewayClient
from pal.minion.llm_broker import (
    MinionBrokerLLMRuntime,
    llm_outcome_from_payload,
    llm_outcome_to_payload,
    llm_request_from_payload,
    llm_request_to_payload,
    preflight_advice_from_payload,
    preflight_advice_to_payload,
    preflight_request_from_payload,
    preflight_request_to_payload,
)
from pal.minion.runner import (
    MinionRunner,
    MinionRuntimeBundle,
    _minion_temperature,
    _resolve_minion_max_output_tokens,
)
from pal.minion.prompt_adapter import render_minion_task_prompt
from pal.minion.git_shim import GIT_TRAP_EXIT_CODE, _RoleGatewayClient, main as git_shim_main
from pal.minion.user_interaction import (
    DEFAULT_CLARIFICATION_TIMEOUT_SECONDS,
    MinionUserInteractionPort,
)
from pal.minion.v2.worker_main import _read_control_message
from pal.minion.sandbox import (
    PAL_MINION_RUNTIME_ROOT_ENV,
    PAL_MINION_TOOL_RESULT_ROOT_ENV,
    build_sandboxed_runner_invocation,
    ensure_sandbox_files,
    minion_sandbox_scratch_dir,
    scrub_minion_sandbox_env,
    with_minion_sandbox_metadata,
)
from pal.shared import RuntimeStatus, MinionInvocationPack


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout or f"git {' '.join(args)} failed")
    return result


def _prepare_role_endpoint(runtime_root: Path) -> Path:
    path = runtime_root / "data" / "minion-role" / "role.sock"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("test endpoint", encoding="utf-8")
    return path


class MinionSandboxTests(unittest.TestCase):
    def test_question_waits_for_matching_user_response(self) -> None:
        async def scenario() -> None:
            events: list[dict[str, object]] = []
            responses: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            requested = asyncio.Event()
            observed_timeouts: list[float | None] = []

            async def emit_event(kind: str, payload: dict[str, object]) -> None:
                events.append({"kind": kind, "payload": dict(payload)})
                if kind == "clarification_requested":
                    requested.set()

            async def read_response(timeout: float | None) -> dict[str, object]:
                observed_timeouts.append(timeout)
                return await responses.get()

            port = MinionUserInteractionPort(
                emit_event=emit_event,
                read_response=read_response,
                run_id="run-question",
                minion_id="minion-question",
                invocation_id="inv-question",
                workflow_id="wf-question",
                control_route={
                    "endpoint_id": "socket",
                    "channel_kind": "socket",
                    "reply_target": {"connection_id": "client-1"},
                    "control_scope_key": "socket:client-1",
                },
            )
            pending = asyncio.create_task(
                port.request_clarification(
                    {
                        "title": "Choose compatibility",
                        "questions": [
                            {
                                "id": "compatibility",
                                "question": "Which boundary is binding?",
                                "options": [
                                    {"label": "Preserve", "description": "Keep the API"},
                                    {"label": "Adapt", "description": "Add a facade"},
                                ],
                            }
                        ],
                    },
                    approval_policy={},
                )
            )
            await asyncio.wait_for(requested.wait(), timeout=1)
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            clarification = dict(events[0]["payload"])
            self.assertEqual(clarification["workflow_id"], "wf-question")
            self.assertEqual(
                clarification["control_route"],
                {
                    "endpoint_id": "socket",
                    "channel_kind": "socket",
                    "reply_target": {"connection_id": "client-1"},
                    "control_scope_key": "socket:client-1",
                },
            )
            await responses.put(
                {
                    "type": "clarification",
                    "clarification": {
                        "clarification_id": clarification["clarification_id"],
                        "answers": [
                            {"question_id": "compatibility", "answer": "Preserve"}
                        ],
                    },
                }
            )

            response = await asyncio.wait_for(pending, timeout=1)

            self.assertEqual(response["answers"][0]["answer"], "Preserve")
            self.assertEqual(
                observed_timeouts,
                [float(DEFAULT_CLARIFICATION_TIMEOUT_SECONDS)],
            )
            self.assertEqual(
                [item["kind"] for item in events],
                ["clarification_requested", "clarification_received"],
            )

        asyncio.run(scenario())

    def test_worker_control_queue_none_timeout_blocks_until_message(self) -> None:
        async def scenario() -> None:
            messages: asyncio.Queue[dict[str, object]] = asyncio.Queue()
            pending = asyncio.create_task(_read_control_message(messages, None))
            await asyncio.sleep(0.01)
            self.assertFalse(pending.done())
            await messages.put({"type": "clarification"})
            self.assertEqual(
                await asyncio.wait_for(pending, timeout=1),
                {"type": "clarification"},
            )

        asyncio.run(scenario())

    def test_sandboxed_broker_requires_assignment_gateway_token(self) -> None:
        runtime = MinionBrokerLLMRuntime(
            Path("/tmp/pal-minion-broker-token"),
            run_id="run-token",
        )
        with patch.dict(
            os.environ,
            {
                "PAL_MINION_SANDBOXED": "1",
                ROLE_GATEWAY_TOKEN_ENV: "",
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "assignment-scoped"):
                _ = runtime._client

    def test_sandboxed_broker_uses_assignment_role_gateway_token(self) -> None:
        runtime = MinionBrokerLLMRuntime(
            Path("/tmp/pal-minion-broker-token"),
            run_id="run-token",
        )
        with patch.dict(
            os.environ,
            {
                "PAL_MINION_SANDBOXED": "1",
                ROLE_GATEWAY_TOKEN_ENV: "assignment-only",
            },
        ):
            client = runtime._client

        self.assertIsInstance(client, MinionRoleGatewayClient)
        self.assertEqual(client.access_token, "assignment-only")

    def test_sandboxed_lsp_client_requires_unix_transport(self) -> None:
        with patch.dict(
            os.environ,
            {"PAL_MINION_SANDBOXED": "1"},
            clear=False,
        ):
            client = LspManagerClient(Path("/tmp/pal-minion-lsp-uds"))

        self.assertTrue(client._client.unix_only)

    def test_minion_temperature_accepts_low_deterministic_profile_value(self) -> None:
        pack = MinionInvocationPack(invocation_id="temperature", goal="g", metadata={"temperature": 0.05})
        self.assertEqual(_minion_temperature(pack, fallback=0.7), 0.05)
        invalid = MinionInvocationPack(invocation_id="temperature-invalid", goal="g", metadata={"temperature": 3})
        self.assertEqual(_minion_temperature(invalid, fallback=0.7), 0.7)

    def test_minion_output_budget_is_capped_by_endpoint_limit(self) -> None:
        class Runtime:
            def resolve_max_output_tokens(self, **_kwargs):
                return 12_288

        runtime = Runtime()
        oversized = MinionInvocationPack(
            invocation_id="oversized-output-budget",
            goal="g",
            metadata={"max_output_tokens": 65_536},
        )
        bounded = MinionInvocationPack(
            invocation_id="bounded-output-budget",
            goal="g",
            metadata={"max_output_tokens": 8_192},
        )

        self.assertEqual(_resolve_minion_max_output_tokens(runtime, oversized), 12_288)
        self.assertEqual(_resolve_minion_max_output_tokens(runtime, bounded), 8_192)

    def test_minion_output_budget_uses_endpoint_limit_without_profile_override(self) -> None:
        class Runtime:
            def resolve_max_output_tokens(self, **_kwargs):
                return 12_288

        pack = MinionInvocationPack(invocation_id="endpoint-output-budget", goal="g")

        self.assertEqual(_resolve_minion_max_output_tokens(Runtime(), pack), 12_288)

    def test_sandbox_metadata_defaults_to_bubblewrap(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_meta_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            pack = MinionInvocationPack(invocation_id="wo", goal="g", workspace={"repo_path": str(repo)})

            with (
                patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")}),
                patch("pal.minion.sandbox.sandbox_supported_backend", return_value="bwrap"),
            ):
                updated = with_minion_sandbox_metadata(root, pack, run_id="run_1")

            sandbox = updated.metadata["sandbox"]
            self.assertTrue(sandbox["enabled"])
            self.assertEqual(sandbox["backend"], "bwrap")
            self.assertEqual(sandbox["workspace_path"], str(repo))
            self.assertEqual(sandbox["secret_policy"], "host_llm_broker")
            self.assertEqual(sandbox["scratch_dir"], str(root / "tmp_scratch" / "run_1"))
            self.assertEqual(sandbox["network"], "isolated")
            self.assertNotIn("blacklist_commands", sandbox)

    def test_sandbox_metadata_rejects_unwired_backend(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_backend_") as tmp:
            pack = MinionInvocationPack(
                invocation_id="wo",
                goal="g",
                metadata={"sandbox": {"backend": "docker"}},
            )

            with self.assertRaisesRegex(RuntimeError, "unsupported backend: docker"):
                with_minion_sandbox_metadata(Path(tmp), pack, run_id="run_backend")

    def test_all_minion_execution_fails_closed_without_supported_backend(self) -> None:
        pack = MinionInvocationPack(invocation_id="no-sandbox", goal="inspect")

        with patch("pal.minion.sandbox.sandbox_supported_backend", return_value=""):
            with self.assertRaisesRegex(RuntimeError, "requires bubblewrap"):
                with_minion_sandbox_metadata(Path("/tmp"), pack, run_id="no-sandbox")

    def test_runner_invocation_rejects_missing_sandbox_metadata(self) -> None:
        pack = MinionInvocationPack(invocation_id="missing-sandbox", goal="inspect")

        with self.assertRaisesRegex(RuntimeError, "missing its required OS sandbox"):
            build_sandboxed_runner_invocation(
                runtime_root=Path("/tmp"),
                pack=pack,
                argv=["python", "-c", "pass"],
            )

    def test_runner_invocation_fails_closed_without_unix_role_gateway(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_missing_uds_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            pack = MinionInvocationPack(
                invocation_id="missing-uds",
                goal="inspect",
                workspace={"repo_path": str(repo)},
                metadata={
                    "sandbox": {
                        "enabled": True,
                        "backend": "bwrap",
                        "run_id": "missing-uds",
                    }
                },
            )

            with self.assertRaisesRegex(RuntimeError, "Unix role gateway"):
                build_sandboxed_runner_invocation(
                    runtime_root=root,
                    pack=pack,
                    argv=["python", "-c", "pass"],
                    env={"PATH": "/usr/bin:/bin"},
                )

    def test_sandbox_metadata_does_not_project_external_git_internals(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_gitmeta_") as tmp:
            root = Path(tmp)
            workspace = root / "repo"
            workspace.mkdir()
            with (
                patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "scratch")}),
                patch("pal.minion.sandbox.sandbox_supported_backend", return_value="bwrap"),
            ):
                pack = with_minion_sandbox_metadata(
                    root / "runtime",
                    MinionInvocationPack(
                        invocation_id="gitmeta",
                        goal="inspect",
                        workspace={"repo_path": str(workspace)},
                    ),
                    run_id="gitmeta",
                )

            self.assertNotIn("git_metadata_bind_paths", pack.metadata["sandbox"])

    def test_sandbox_env_scrubs_secret_like_values_and_enables_broker(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_env_") as tmp:
            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(Path(tmp) / "tmp_scratch")}):
                env = scrub_minion_sandbox_env(
                    {
                        "PATH": "/usr/bin",
                        "OPENAI_API_KEY": "secret",
                        "NORMAL_VALUE": "kept",
                        "PAL_TOKEN": "secret",
                        ROLE_GATEWAY_TOKEN_ENV: "assignment-only",
                    },
                    runtime_root=Path(tmp),
                    run_id="run_env",
                )

            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertEqual(env["NORMAL_VALUE"], "kept")
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertNotIn("PAL_TOKEN", env)
            self.assertEqual(env[ROLE_GATEWAY_TOKEN_ENV], "assignment-only")
            self.assertEqual(env["PAL_MINION_LLM_BROKER"], "1")
            self.assertEqual(env["PAL_MINION_WEB_BROKER"], "1")
            self.assertEqual(env["PAL_DATABASE_READ_ONLY"], "1")
            self.assertEqual(env["PAL_MINION_SANDBOXED"], "1")
            self.assertEqual(env["HOME"], "/tmp/home")
            self.assertEqual(env["TMPDIR"], "/tmp")
            self.assertEqual(env["XDG_CACHE_HOME"], "/tmp/cache")
            self.assertEqual(env["PYTHONPYCACHEPREFIX"], "/tmp/pycache")

    def test_sandbox_env_marks_only_continuation_attempts_as_retries(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_retry_env_") as tmp:
            retry_pack = MinionInvocationPack(
                invocation_id="retry",
                metadata={
                    "agent_session": {
                        "continuation_input_path": str(Path(tmp) / "continuation.json")
                    }
                },
            )
            retry_env = scrub_minion_sandbox_env(
                {},
                runtime_root=Path(tmp),
                run_id="run_retry",
                pack=retry_pack,
            )
            fresh_env = scrub_minion_sandbox_env(
                {"PAL_MINION_CONTINUATION_RETRY": "1"},
                runtime_root=Path(tmp),
                run_id="run_fresh",
                pack=MinionInvocationPack(invocation_id="fresh"),
            )

        self.assertEqual(retry_env["PAL_MINION_CONTINUATION_RETRY"], "1")
        self.assertNotIn("PAL_MINION_CONTINUATION_RETRY", fresh_env)

    def test_sandbox_env_applies_workspace_execution_env(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_workspace_env_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            src = repo / "src"
            src.mkdir(parents=True)
            pack = MinionInvocationPack(
                invocation_id="wo",
                goal="g",
                workspace={
                    "repo_path": str(repo),
                    "build_scratch_dir": str(root / "build-scratch"),
                    "review_scratch_dir": str(root / "review-scratch"),
                    "execution_env": {
                        "vars": {
                            "CMAKE_EXPORT_COMPILE_COMMANDS": "ON",
                            "PAL_TOKEN": "secret",
                        },
                        "path_prepend": {"PYTHONPATH": [str(src)]},
                    },
                },
            )

            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")}):
                env = scrub_minion_sandbox_env(
                    {"PATH": "/usr/bin", "PYTHONPATH": "/existing"},
                    runtime_root=root,
                    run_id="run_workspace_env",
                    pack=pack,
                )

            self.assertEqual(env["PYTHONPATH"].split(os.pathsep)[0], str(src))
            self.assertIn("/existing", env["PYTHONPATH"].split(os.pathsep))
            self.assertEqual(env["CMAKE_EXPORT_COMPILE_COMMANDS"], "ON")
            self.assertEqual(env["PAL_WORKSPACE_ROOT"], str(repo))
            self.assertEqual(env["PAL_BUILD_SCRATCH"], str(root / "build-scratch"))
            self.assertEqual(env["PAL_REVIEW_SCRATCH"], str(root / "review-scratch"))
            self.assertNotIn("PAL_TOKEN", env)
            self.assertIn("PYTHONUSERBASE", env)

    def test_sandbox_scratch_and_git_shims_are_created(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_wrappers_") as tmp:
            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(Path(tmp) / "tmp_scratch")}):
                scratch, shim_dir = ensure_sandbox_files(
                    Path(tmp),
                    run_id="run_wrap",
                )

            self.assertTrue((scratch / "tmp").is_dir())
            self.assertTrue((scratch / "tmp" / "home").is_dir())
            self.assertTrue((scratch / "tmp" / "cache").is_dir())
            self.assertTrue((scratch / "tmp" / "pycache").is_dir())
            self.assertEqual(scratch, Path(tmp) / "tmp_scratch" / "run_wrap")
            self.assertEqual(shim_dir.name, "shim-bin")
            self.assertFalse((shim_dir / "rm").exists())
            self.assertFalse((shim_dir / "unlink").exists())
            git_wrapper = shim_dir / "git"
            self.assertTrue(os.access(git_wrapper, os.X_OK))
            wrapper_text = git_wrapper.read_text(encoding="utf-8")
            self.assertIn("/usr/bin/python3", wrapper_text)
            self.assertIn("git_shim.py", wrapper_text)
            self.assertNotIn("pal.minion.git_shim", wrapper_text)
            git_internal = shim_dir / "git-internal"
            self.assertTrue(os.access(git_internal, os.X_OK))
            self.assertIn("blocked internal Git entry point", git_internal.read_text(encoding="utf-8"))

    def test_git_shim_delegates_reads_and_rejects_every_other_classification(self) -> None:
        class Client:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, str]]] = []

            def request_sync(self, method: str, params: dict[str, str]) -> dict[str, object]:
                self.calls.append((method, dict(params)))
                policy = classify_git_command(params.get("cmd"))
                if policy.operation_kind != "read":
                    raise RuntimeError(
                        "only classified read-only Git commands are allowed: "
                        + (policy.reason or "blocked")
                    )
                return {"returncode": 0, "stdout": " M src/router.py\n", "stderr": ""}

        client = Client()
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {PAL_MINION_RUNTIME_ROOT_ENV: "/runtime"}),
            patch("pal.minion.git_shim.role_gateway_client_from_env", return_value=client),
            patch("pal.minion.git_shim.os.getcwd", return_value="/repo"),
            patch("sys.stdout", stdout),
        ):
            returncode = git_shim_main(["status", "--short"])

        self.assertEqual(returncode, 0)
        self.assertEqual(stdout.getvalue(), " M src/router.py\n")
        self.assertEqual(
            client.calls,
            [("git_read", {"cmd": "status --short", "cwd": "/repo"})],
        )

        with (
            patch.dict(os.environ, {PAL_MINION_RUNTIME_ROOT_ENV: "/runtime"}),
            patch("pal.minion.git_shim.role_gateway_client_from_env", return_value=client),
            patch("pal.minion.git_shim.os.getcwd", return_value="/repo"),
            patch("sys.stdout", io.StringIO()),
        ):
            returncode = git_shim_main(["--no-pager", "-C", "nested", "diff", "--stat"])

        self.assertEqual(returncode, 0)
        self.assertEqual(
            client.calls[-1],
            ("git_read", {"cmd": "diff --stat", "cwd": "/repo/nested"}),
        )

        for command in (["restore", "--", "src/router.py"], ["commit", "-am", "x"], ["frobnicate"]):
            with (
                self.subTest(command=command),
                patch.dict(os.environ, {PAL_MINION_RUNTIME_ROOT_ENV: "/runtime"}),
                patch("pal.minion.git_shim.role_gateway_client_from_env", return_value=client),
                patch("sys.stderr", io.StringIO()) as stderr,
            ):
                returncode = git_shim_main(command)
                self.assertEqual(returncode, GIT_TRAP_EXIT_CODE)
                self.assertIn("only classified read-only", stderr.getvalue())
        with patch("sys.stderr", io.StringIO()) as stderr:
            self.assertEqual(git_shim_main(["-C"]), GIT_TRAP_EXIT_CODE)
            self.assertIn("requires a directory", stderr.getvalue())
        self.assertEqual(len(client.calls), 5)

    def test_lightweight_git_shim_uses_role_gateway_wire_protocol(self) -> None:
        client_socket, server_socket = socket.socketpair()
        observed: list[dict[str, object]] = []

        def serve() -> None:
            try:
                raw_size = server_socket.recv(4)
                size = int.from_bytes(raw_size, "big")
                raw_request = bytearray()
                while len(raw_request) < size:
                    raw_request.extend(server_socket.recv(size - len(raw_request)))
                request = msgpack.unpackb(bytes(raw_request), raw=False)
                observed.append(dict(request))
                response = msgpack.packb(
                    {
                        "type": "response",
                        "id": request["id"],
                        "ok": True,
                        "result": {"returncode": 0, "stdout": "main\n", "stderr": ""},
                    },
                    use_bin_type=True,
                )
                server_socket.sendall(len(response).to_bytes(4, "big") + response)
            finally:
                server_socket.close()

        thread = threading.Thread(target=serve)
        thread.start()
        try:
            with patch("pal.minion.git_shim._open_role_gateway", return_value=client_socket):
                result = _RoleGatewayClient(Path("/runtime"), "assignment-token").request_sync(
                    "git_read",
                    {"cmd": "status --short", "cwd": "/repo"},
                )
        finally:
            thread.join(timeout=2)

        self.assertEqual(result["stdout"], "main\n")
        self.assertEqual(observed[0]["method"], "git_read")
        params = dict(observed[0]["params"])
        self.assertEqual(params["cmd"], "status --short")
        self.assertEqual(params["cwd"], "/repo")
        self.assertEqual(params["access_token"], "assignment-token")

    def test_runner_invocation_uses_broker_env_when_sandboxed(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_invocation_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            _prepare_role_endpoint(root)
            pack = MinionInvocationPack(
                invocation_id="wo",
                goal="g",
                workspace={"repo_path": str(repo)},
                metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "run_inv"}},
            )

            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")}):
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=root,
                    pack=pack,
                    argv=["python", "-m", "pal.minion.v2.worker_main"],
                    env={"PATH": "/usr/bin", "OPENAI_API_KEY": "secret"},
                )

            self.assertTrue(argv[0].endswith("bwrap"))
            self.assertIn("--unshare-user", argv)
            self.assertIn("--disable-userns", argv)
            self.assertIn("--unshare-net", argv)
            self.assertIn("--dev", argv)
            self.assertNotIn("--share-net", argv)
            self.assertNotIn("--dev-bind", argv)
            self.assertIn("--chdir", argv)
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertEqual(env["PAL_MINION_LLM_BROKER"], "1")
            self.assertEqual(env["PAL_MINION_WEB_BROKER"], "1")
            self.assertIn("PYTHONPATH", env)

    def test_sandbox_projects_writable_temp_and_home_paths(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_temp_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            _prepare_role_endpoint(root)
            pack = MinionInvocationPack(
                invocation_id="temp-paths",
                goal="probe sandbox temp paths",
                workspace={"repo_path": str(repo)},
                metadata={
                    "sandbox": {
                        "enabled": True,
                        "backend": "bwrap",
                        "run_id": "temp-paths",
                    }
                },
            )
            probe = (
                "set -eu; "
                'pwd -P; '
                'test \"$TMPDIR\" = /tmp; '
                'test \"$HOME\" = /tmp/home; '
                'test -d \"$HOME\"; '
                'test -d \"$XDG_CACHE_HOME\"; '
                'test -d \"$PYTHONPYCACHEPREFIX\"; '
                'made=\"$(mktemp -d)\"; '
                'case \"$made\" in /tmp/*) ;; *) exit 31 ;; esac; '
                'printf ok > \"$HOME/write-probe\"; '
                'printf \"%s\\n\" \"$made\"'
            )

            with patch.dict(
                os.environ,
                {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")},
            ):
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=root,
                    pack=pack,
                    argv=["/bin/sh", "-lc", probe],
                    env={"PATH": "/usr/bin:/bin"},
                )
                result = subprocess.run(
                    argv,
                    env=env,
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            output_lines = result.stdout.splitlines()
            self.assertEqual(output_lines[0], str(repo.resolve()))
            self.assertTrue(output_lines[1].startswith("/tmp/"))

    def test_sandbox_blocks_host_network_and_nested_user_namespaces(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_network_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            _prepare_role_endpoint(root)
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            pack = MinionInvocationPack(
                invocation_id="isolated-network",
                goal="probe sandbox isolation",
                workspace={"repo_path": str(repo)},
                metadata={
                    "sandbox": {
                        "enabled": True,
                        "backend": "bwrap",
                        "run_id": "isolated-network",
                    }
                },
            )
            probe = (
                "import socket,subprocess; "
                f"s=socket.socket(); s.settimeout(1); rc=0; "
                f"\ntry: s.connect(('127.0.0.1',{port})); rc=31"
                "\nexcept OSError: pass"
                "\nfinally: s.close()"
                "\nif rc: raise SystemExit(rc)"
                "\nif subprocess.run(['unshare','-Ur','true']).returncode == 0: "
                "raise SystemExit(32)"
                "\nprint('isolated')"
            )
            try:
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=root,
                    pack=pack,
                    argv=["python", "-c", probe],
                    env={"PATH": "/usr/bin:/bin"},
                )
                result = subprocess.run(
                    argv,
                    env=env,
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            finally:
                listener.close()

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("isolated", result.stdout)

    def test_unshared_network_keeps_assignment_unix_gateway_reachable(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_uds_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            endpoint = root / "data" / "minion-role" / "role.sock"
            endpoint.parent.mkdir(parents=True)
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(endpoint))
            server.listen(1)

            def serve() -> None:
                connection, _ = server.accept()
                with connection:
                    self.assertEqual(connection.recv(4), b"ping")
                    connection.sendall(b"pong")

            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            pack = MinionInvocationPack(
                invocation_id="uds-network",
                goal="probe role gateway",
                workspace={"repo_path": str(repo)},
                metadata={
                    "sandbox": {
                        "enabled": True,
                        "backend": "bwrap",
                        "run_id": "uds-network",
                    }
                },
            )
            probe = (
                "import socket; "
                "s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); "
                f"s.connect({str(endpoint)!r}); s.sendall(b'ping'); "
                "print(s.recv(4).decode()); s.close()"
            )
            try:
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=root,
                    pack=pack,
                    argv=["python", "-c", probe],
                    env={"PATH": "/usr/bin:/bin"},
                )
                result = subprocess.run(
                    argv,
                    env=env,
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
            finally:
                server.close()
                thread.join(timeout=2)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "pong")

    def test_role_worktree_allows_ordinary_shell_deletion(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_deny_bin_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            sentinel = repo / "keep.txt"
            sentinel.write_text("keep\n", encoding="utf-8")
            _prepare_role_endpoint(root)
            with patch.dict(
                os.environ,
                {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")},
            ):
                pack = with_minion_sandbox_metadata(
                    root,
                    MinionInvocationPack(
                        invocation_id="delete",
                        goal="verify worktree deletion",
                        workspace={"repo_path": str(repo)},
                    ),
                    run_id="delete",
                )
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=root,
                    pack=pack,
                    argv=["/bin/sh", "-c", "rm keep.txt"],
                    env={"PATH": "/usr/bin:/bin"},
                )
            result = subprocess.run(
                argv,
                env=env,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(sentinel.exists())

    def test_reference_projection_uses_stable_read_only_sandbox_path(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_reference_") as tmp:
            root = Path(tmp)
            runtime_root = root / "runtime"
            repo = root / "repo"
            reference = root / "reference"
            repo.mkdir()
            reference.mkdir()
            _prepare_role_endpoint(runtime_root)
            (reference / "task.yaml").write_text("framepipe\n", encoding="utf-8")
            (reference / "manifest.json").write_text('{"version":1}\n', encoding="utf-8")
            (reference / "private.txt").write_text("not projected\n", encoding="utf-8")
            pack = MinionInvocationPack(
                invocation_id="reference_projection",
                goal="inspect task",
                workspace={
                    "repo_path": str(repo),
                    "reference_paths": [
                        {
                            "name": "task",
                            "path": str(reference / "*.yaml"),
                            "truth_source": True,
                            "required": True,
                        },
                        {
                            "name": "architecture_index",
                            "path": str(reference / "manifest.json"),
                            "truth_source": True,
                            "required": True,
                        },
                    ],
                },
            )
            with patch.dict(
                os.environ,
                {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "scratch")},
            ):
                pack = with_minion_sandbox_metadata(
                    runtime_root,
                    pack,
                    run_id="reference_projection",
                )
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=runtime_root,
                    pack=pack,
                    argv=[
                        "/bin/sh",
                        "-c",
                        "tree -a -L 3 --filelimit 200 --noreport /pal/references/task; "
                        "test -r /pal/references/task/task.yaml; "
                        "test -f /pal/references/architecture_index/manifest.json; "
                        "test ! -e /pal/references/task/private.txt; "
                        "if printf changed > /pal/references/task/task.yaml 2>/dev/null; then exit 31; fi; "
                        "if printf new > /pal/references/task/new.txt 2>/dev/null; then exit 32; fi",
                    ],
                    env={"PATH": "/usr/bin:/bin"},
                )

            projected = dict(pack.workspace["reference_paths"][0])
            self.assertEqual(projected["path"], "/pal/references/task")
            self.assertEqual(
                pack.workspace["reference_paths"][1]["path"],
                "/pal/references/architecture_index/manifest.json",
            )
            rebound = with_minion_sandbox_metadata(
                runtime_root,
                pack,
                run_id="reference_projection",
            )
            self.assertEqual(
                rebound.workspace["reference_paths"][0]["path"],
                "/pal/references/task",
            )
            self.assertEqual(
                rebound.metadata["sandbox"]["reference_binds"][0]["source_path"],
                str(reference),
            )
            prompt = render_minion_task_prompt(pack)
            self.assertIn("reference:task: read-only semantic input", prompt)
            self.assertIn("path=/pal/references/task", prompt)
            self.assertIn('read_file_args={"file_path":"/pal/references/architecture_index/manifest.json"}', prompt)
            self.assertIn("## Tool Efficiency", prompt)
            self.assertIn("investigate what the supplied path currently contains", prompt)
            self.assertIn("Do not assume the path is a file", prompt)
            self.assertIn("use that exact file path directly", prompt)
            self.assertNotIn("reference pack root is a directory", prompt)
            self.assertIn("Immutable inputs are lookup sources, not a mandatory reading checklist", prompt)
            self.assertNotIn("tree -a", prompt)
            self.assertNotIn("find ", prompt)

            implementation_prompt = render_minion_task_prompt(
                MinionInvocationPack.from_dict(
                    {
                        **pack.to_dict(),
                        "metadata": {"minion_v2": {"role": "implementation"}},
                    }
                )
            )
            self.assertIn("Once the owned contract, edit path", implementation_prompt)
            self.assertIn("Do not over-abstract", implementation_prompt)

            result = subprocess.run(
                argv,
                env=env,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("task.yaml", result.stdout)
            self.assertNotIn("private.txt", result.stdout)
            self.assertEqual((reference / "task.yaml").read_text(encoding="utf-8"), "framepipe\n")
            self.assertFalse((reference / "new.txt").exists())

    def test_read_only_workspace_is_mounted_read_only(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_read_only_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            _prepare_role_endpoint(root)
            pack = MinionInvocationPack(
                invocation_id="wo_read_only",
                goal="review",
                workspace={
                    "repo_path": str(repo),
                    "workspace_policy": {"mode": "read_only_repo"},
                },
                metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "run_read_only"}},
            )

            argv, _env = build_sandboxed_runner_invocation(
                runtime_root=root,
                pack=pack,
                argv=[
                    "/bin/sh",
                    "-c",
                    "if printf changed > reviewer-write.txt 2>/dev/null; then exit 41; fi",
                ],
                env={"PATH": "/usr/bin:/bin"},
            )

            self.assertTrue(
                any(
                    argv[index : index + 3] == ["--ro-bind", str(repo), str(repo)]
                    for index in range(max(0, len(argv) - 2))
                )
            )
            result = subprocess.run(
                argv,
                env=_env,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((repo / "reviewer-write.txt").exists())

    def test_worker_sees_read_only_pal_db_and_only_its_minion_runtime_slice(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_runtime_scope_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            run_dir = root / "data" / "minion" / "runtime" / "invocations" / "attempt-1"
            run_dir.mkdir(parents=True)
            minion_db = root / "data" / "minion" / "minion.sqlite3"
            minion_db.write_text("private", encoding="utf-8")
            pal_db = root / "pal.sqlite3"
            pal_db.write_text("memory", encoding="utf-8")
            role_socket = root / "data" / "minion-role" / "role.sock"
            role_socket.parent.mkdir(parents=True)
            role_socket.write_text("endpoint", encoding="utf-8")
            pack = MinionInvocationPack(
                invocation_id="attempt-1",
                goal="work",
                workspace={"repo_path": str(repo), "run_dir": str(run_dir)},
                metadata={
                    "sandbox": {
                        "enabled": True,
                        "backend": "bwrap",
                        "run_id": "runtime-scope",
                    }
                },
            )

            argv, _env = build_sandboxed_runner_invocation(
                runtime_root=root,
                pack=pack,
                argv=["python", "-c", "pass"],
                env={"PATH": "/usr/bin:/bin", ROLE_GATEWAY_TOKEN_ENV: "token"},
            )

            self.assertEqual(_env[ROLE_GATEWAY_TOKEN_ENV], "token")

            triples = [argv[index : index + 3] for index in range(max(0, len(argv) - 2))]
            self.assertIn(["--ro-bind", str(pal_db), str(pal_db)], triples)
            self.assertIn(["--ro-bind", str(role_socket), str(role_socket)], triples)
            self.assertIn(["--bind", str(run_dir), str(run_dir)], triples)
            self.assertEqual(
                _env[PAL_MINION_TOOL_RESULT_ROOT_ENV],
                str(run_dir / "tool-results"),
            )
            self.assertNotIn(str(root / "data" / "tool_results"), argv)
            self.assertNotIn(str(root / "artifacts"), argv)
            self.assertNotIn(
                ["--bind", str(root / "data" / "minion"), str(root / "data" / "minion")],
                triples,
            )
            self.assertNotIn(str(minion_db), argv)

    def test_semantic_write_scopes_do_not_fragment_the_role_worktree_mount(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_scoped_") as tmp:
            root = Path(tmp)
            runtime_root = root / "runtime"
            repo = root / "repo"
            (repo / "contracts").mkdir(parents=True)
            (repo / "src" / "private").mkdir(parents=True)
            (repo / "tests").mkdir()
            (repo / "contracts" / "router.py").write_text("contract\n", encoding="utf-8")
            (repo / "src" / "router.py").write_text("impl\n", encoding="utf-8")
            (repo / "src" / "sibling.py").write_text("sibling\n", encoding="utf-8")
            (repo / "src" / "private" / "seed.py").write_text("seed\n", encoding="utf-8")
            (repo / "tests" / "test_router.py").write_text("test\n", encoding="utf-8")
            _prepare_role_endpoint(runtime_root)
            _git(repo, "init")
            _git(repo, "config", "user.email", "pal-test@example.invalid")
            _git(repo, "config", "user.name", "Pal Test")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "initial")
            pack = MinionInvocationPack(
                invocation_id="scoped",
                goal="implement router",
                workspace={
                    "repo_path": str(repo),
                    "write_path_scopes": [
                        {"kind": "file", "path": "src/router.py"},
                        {"kind": "directory", "path": "src/private"},
                        {"kind": "file", "path": "tests/test_router.py"},
                    ],
                    "workspace_policy": {"mode": "writable_git_branch"},
                },
            )
            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "scratch")}):
                pack = with_minion_sandbox_metadata(runtime_root, pack, run_id="run_scoped")
                script = """
printf changed > src/router.py
printf new > src/private/new.py
printf changed-test > tests/test_router.py
rm src/private/seed.py
printf changed-contract > contracts/router.py
printf changed-sibling > src/sibling.py
if printf bad > .git/config 2>/dev/null; then exit 23; fi
rm contracts/router.py
"""
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=runtime_root,
                    pack=pack,
                    argv=["/bin/sh", "-c", script],
                    env={"PATH": "/usr/bin:/bin"},
                )
            result = subprocess.run(argv, env=env, cwd=repo, capture_output=True, text=True, timeout=20)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((repo / "src" / "router.py").read_text(), "changed")
            self.assertEqual((repo / "src" / "private" / "new.py").read_text(), "new")
            self.assertFalse((repo / "src" / "private" / "seed.py").exists())
            self.assertEqual((repo / "tests" / "test_router.py").read_text(), "changed-test")
            self.assertFalse((repo / "contracts" / "router.py").exists())
            self.assertEqual((repo / "src" / "sibling.py").read_text(), "changed-sibling")

    def test_scoped_workspace_materializes_future_coder_paths(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_future_scopes_") as tmp:
            root = Path(tmp)
            runtime_root = root / "runtime"
            repo = root / "repo"
            repo.mkdir()
            _prepare_role_endpoint(runtime_root)
            _git(repo, "init")
            _git(repo, "config", "user.email", "pal-test@example.invalid")
            _git(repo, "config", "user.name", "Pal Test")
            (repo / "README.md").write_text("base\n", encoding="utf-8")
            _git(repo, "add", "README.md")
            _git(repo, "commit", "-m", "initial")
            pack = MinionInvocationPack(
                invocation_id="future-scopes",
                goal="implement future paths",
                workspace={
                    "repo_path": str(repo),
                    "write_path_scopes": [
                        {"kind": "file", "path": "src/router.py"},
                        {"kind": "directory", "path": "generated/router"},
                    ],
                    "workspace_policy": {"mode": "writable_git_branch"},
                },
            )
            with patch.dict(
                os.environ,
                {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "scratch")},
            ):
                pack = with_minion_sandbox_metadata(
                    runtime_root,
                    pack,
                    run_id="run_future_scopes",
                )
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=runtime_root,
                    pack=pack,
                    argv=[
                        "/bin/sh",
                        "-c",
                        "mkdir -p src generated/router && "
                        "printf implementation > src/router.py && "
                        "printf generated > generated/router/data.txt",
                    ],
                    env={"PATH": "/usr/bin:/bin"},
                )

            result = subprocess.run(
                argv,
                env=env,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((repo / "src" / "router.py").read_text(), "implementation")
            self.assertEqual(
                (repo / "generated" / "router" / "data.txt").read_text(),
                "generated",
            )

    def test_verifier_regression_overlay_is_read_only_inside_sandbox(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_overlay_") as tmp:
            root = Path(tmp)
            runtime_root = root / "runtime"
            repo = root / "repo"
            (repo / "src").mkdir(parents=True)
            (repo / "tests").mkdir()
            (repo / "src" / "router.py").write_text("old\n", encoding="utf-8")
            (repo / "tests" / "test_router.py").write_text(
                "def test_router():\n    assert False\n",
                encoding="utf-8",
            )
            _prepare_role_endpoint(runtime_root)
            _git(repo, "init")
            _git(repo, "config", "user.email", "pal-test@example.invalid")
            _git(repo, "config", "user.name", "Pal Test")
            _git(repo, "add", "-A")
            _git(repo, "commit", "-m", "initial")
            pack = MinionInvocationPack(
                invocation_id="repair-overlay",
                goal="repair router without changing verifier tests",
                workspace={
                    "repo_path": str(repo),
                    "write_path_scopes": [
                        {"kind": "directory", "path": "src"},
                        {"kind": "directory", "path": "tests"},
                    ],
                    "read_only_overlay_paths": ["tests/test_router.py"],
                    "workspace_policy": {"mode": "writable_git_branch"},
                },
            )
            with patch.dict(
                os.environ,
                {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "scratch")},
            ):
                pack = with_minion_sandbox_metadata(
                    runtime_root,
                    pack,
                    run_id="run_overlay",
                )
                script = """
printf fixed > src/router.py
if printf pass > tests/test_router.py 2>/dev/null; then exit 41; fi
"""
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=runtime_root,
                    pack=pack,
                    argv=["/bin/sh", "-c", script],
                    env={"PATH": "/usr/bin:/bin"},
                )

            result = subprocess.run(
                argv,
                env=env,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((repo / "src" / "router.py").read_text(), "fixed")
            self.assertIn("assert False", (repo / "tests" / "test_router.py").read_text())

    def test_minion_workspace_fails_closed_when_sandbox_is_disabled(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="scoped-disabled",
            goal="implement",
            workspace={
                "repo_path": "/tmp",
                "write_path_scopes": [{"kind": "directory", "path": "owned"}],
            },
        )
        with patch.dict(os.environ, {"PAL_MINION_SANDBOX": "0"}):
            with self.assertRaisesRegex(RuntimeError, "requires an OS sandbox"):
                with_minion_sandbox_metadata(Path("/tmp"), pack, run_id="disabled")

    def test_sandbox_scratch_prefers_temp_root_and_falls_back_when_unusable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_scratch_") as tmp:
            root = Path(tmp)
            temp_root = root / "tmp_scratch"
            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(temp_root)}):
                self.assertEqual(minion_sandbox_scratch_dir(root, "run_a"), temp_root / "run_a")

            unusable = root / "not_a_dir"
            unusable.write_text("file", encoding="utf-8")
            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(unusable)}):
                self.assertEqual(
                    minion_sandbox_scratch_dir(root, "run_b"),
                    root / "data" / "minion" / "sandbox" / "runs" / "run_b",
                )

            with patch.dict(
                os.environ,
                {
                    "PAL_MINION_SANDBOX_SCRATCH_ROOT": str(temp_root),
                    "PAL_MINION_SANDBOX_MIN_FREE_MB": "999999999",
                },
            ):
                self.assertEqual(
                    minion_sandbox_scratch_dir(root, "run_c"),
                    root / "data" / "minion" / "sandbox" / "runs" / "run_c",
                )

    def test_sandbox_run_dir_gc_keeps_recent_limited_scratch_dirs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_gc_") as tmp:
            root = Path(tmp)
            temp_root = root / "tmp_scratch"
            env = {
                "PAL_MINION_SANDBOX_SCRATCH_ROOT": str(temp_root),
                "PAL_MINION_SANDBOX_MAX_RUN_DIRS": "2",
            }
            with patch.dict(os.environ, env):
                first, _ = ensure_sandbox_files(root, run_id="run_1")
                second, _ = ensure_sandbox_files(root, run_id="run_2")
                third, _ = ensure_sandbox_files(root, run_id="run_3")

            self.assertFalse(first.exists())
            self.assertTrue(second.exists())
            self.assertTrue(third.exists())

    def test_sandbox_mounts_only_the_git_shims(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_git_trap_") as tmp:
            root = Path(tmp)
            runtime_root = root / "runtime"
            workspace = root / "workspace"
            workspace.mkdir()
            _prepare_role_endpoint(runtime_root)
            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")}):
                pack = with_minion_sandbox_metadata(
                    runtime_root,
                    MinionInvocationPack(
                        invocation_id="wo",
                        goal="g",
                        workspace={"repo_path": str(workspace)},
                    ),
                    run_id="run_git_trap",
                )

                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=runtime_root,
                    pack=pack,
                    argv=["python", "-c", "pass"],
                    env={"PATH": "/usr/bin:/bin"},
                )
            wrapper = runtime_root / "data" / "minion" / "sandbox" / "shim-bin" / "git"
            triples = [argv[index : index + 3] for index in range(max(0, len(argv) - 2))]
            self.assertIn(["--ro-bind", str(wrapper), "/usr/bin/git"], triples)
            internal_git = Path("/usr/lib/git-core/git")
            if internal_git.exists():
                self.assertIn(["--ro-bind", str(wrapper), str(internal_git)], triples)
            internal_helper = Path("/usr/lib/git-core/git-commit")
            if internal_helper.exists():
                self.assertIn(
                    [
                        "--ro-bind",
                        str(wrapper.with_name("git-internal")),
                        str(internal_helper),
                    ],
                    triples,
                )
            self.assertEqual(env[PAL_MINION_RUNTIME_ROOT_ENV], str(runtime_root.resolve()))

    def test_sandboxed_python_can_import_runtime_dependencies(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_import_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            _prepare_role_endpoint(root)
            pack = MinionInvocationPack(
                invocation_id="wo",
                goal="g",
                workspace={"repo_path": str(repo)},
                metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "run_import"}},
            )

            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")}):
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=root,
                    pack=pack,
                    argv=["python", "-c", "import msgpack; import pal.foundation.sidecar; print('imports-ok')"],
                    env={"PATH": "/usr/bin:/bin"},
                )
            result = subprocess.run(argv, env=env, cwd=str(repo), capture_output=True, text=True, timeout=20)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("imports-ok", result.stdout)

    def test_sandboxed_broker_receives_role_gateway_token(self) -> None:
        if not shutil.which("bwrap"):
            self.skipTest("bubblewrap is not available")
        with tempfile.TemporaryDirectory(prefix="pal_minion_sandbox_role_token_") as tmp:
            root = Path(tmp)
            repo = root / "repo"
            repo.mkdir()
            _prepare_role_endpoint(root)
            pack = MinionInvocationPack(
                invocation_id="role-token",
                goal="g",
                workspace={"repo_path": str(repo)},
                metadata={"sandbox": {"enabled": True, "backend": "bwrap", "run_id": "run_role_token"}},
            )
            script = (
                "from pathlib import Path; "
                "from pal.minion.llm_broker import MinionBrokerLLMRuntime; "
                "client = MinionBrokerLLMRuntime(Path.cwd(), 'run-role-token')._client; "
                "print(client.access_token)"
            )

            with patch.dict(os.environ, {"PAL_MINION_SANDBOX_SCRATCH_ROOT": str(root / "tmp_scratch")}):
                argv, env = build_sandboxed_runner_invocation(
                    runtime_root=root,
                    pack=pack,
                    argv=["python", "-c", script],
                    env={"PATH": "/usr/bin:/bin", ROLE_GATEWAY_TOKEN_ENV: "assignment-only"},
                )
            result = subprocess.run(argv, env=env, cwd=str(repo), capture_output=True, text=True, timeout=20)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "assignment-only")

    def test_sandboxed_runner_honors_explicit_approval_policy(self) -> None:
        async def scenario() -> None:
            calls: list[str] = []
            events: list[dict] = []
            pack = MinionInvocationPack(
                invocation_id="wo_sandbox_shell",
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
                events.append(event)

            async def read_decision(timeout):
                _ = timeout
                return {"decision": {"decision": "accept"}}

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
            self.assertEqual(len([event for event in events if event["event_kind"] == "approval_requested"]), 1)

        asyncio.run(scenario())

    def test_runner_preserves_provider_alias_after_policy_admission(self) -> None:
        async def scenario() -> None:
            calls: list[str] = []
            pack = MinionInvocationPack(
                invocation_id="provider_alias",
                goal="inspect task sources",
                allowed_capabilities=["op_file_read"],
            )

            class AliasExecution:
                def resolve_capability_address(self, name):
                    return {"read_file": "op_file_read"}.get(str(name), str(name))

                async def execute_tool_async(self, call, **kwargs):
                    _ = kwargs
                    calls.append(call.name)
                    return CanonicalToolResult(
                        name=call.name,
                        ok=True,
                        text="read ok",
                        llm_text="read ok",
                        structured={"content": "frame"},
                        status=RuntimeStatus.OK,
                    )

            async def write_event(_event):
                return None

            async def read_decision(_timeout):
                return None

            runner = MinionRunner(
                runtime_root=Path(tempfile.mkdtemp(prefix="pal_minion_provider_alias_")),
                pack=pack,
                minion_id="m_provider_alias",
                run_id="r_provider_alias",
                write_event=write_event,
                read_decision=read_decision,
                runtime_bundle=MinionRuntimeBundle(llm_runtime=SimpleNamespace(), execution_runtime=SimpleNamespace()),
            )

            result = await runner._execute_allowed_tool(
                AliasExecution(),
                CanonicalToolCall(
                    name="read_file",
                    args={"file_path": "/pal/references/task.yaml"},
                    call_id="call_read",
                ),
            )

            self.assertTrue(result.ok, result.text)
            self.assertEqual(calls, ["read_file"])

        asyncio.run(scenario())

    def test_runner_binds_lsp_calls_to_isolated_workspace(self) -> None:
        workspace = Path(tempfile.mkdtemp(prefix="pal_minion_lsp_workspace_"))
        runner = MinionRunner(
            runtime_root=workspace.parent,
            pack=MinionInvocationPack(
                invocation_id="inv_lsp",
                goal="inspect code",
                workspace={
                    "repo_path": str(workspace),
                    "primary_language": "cpp",
                    "languages": ["c", "cpp"],
                },
            ),
            minion_id="m_lsp",
            run_id="r_lsp",
            write_event=lambda _event: None,  # type: ignore[arg-type]
            read_decision=lambda _timeout: None,  # type: ignore[arg-type]
        )

        direct = runner._tool_call_with_minion_defaults(
            CanonicalToolCall(name="op_lsp_definition", args={"file": "src/main.cpp", "line": 1, "character": 2})
        )
        nested = runner._tool_call_with_minion_defaults(
            CanonicalToolCall(
                name="op_tool_call",
                args={
                    "name": "op_lsp_diagnostics",
                    "args": {"file": "src/main.cpp"},
                },
            )
        )
        status = runner._tool_call_with_minion_defaults(
            CanonicalToolCall(name="op_lsp_status", args={})
        )
        shell = runner._tool_call_with_minion_defaults(
            CanonicalToolCall(name="op_exec_shell", args={"cmd": "pwd"})
        )
        explicit_shell = runner._tool_call_with_minion_defaults(
            CanonicalToolCall(
                name="op_exec_shell",
                args={"cmd": "pwd", "cwd": str(workspace / "src")},
            )
        )

        self.assertEqual(direct.args["workspace_root"], str(workspace))
        self.assertEqual(status.args["workspace_root"], str(workspace))
        self.assertEqual(shell.args["cwd"], str(workspace))
        self.assertEqual(explicit_shell.args["cwd"], str(workspace / "src"))
        self.assertNotIn("primary_language", direct.args)
        self.assertNotIn("lsp_setup", direct.args)
        nested_args = dict(nested.args["args"])
        self.assertEqual(nested_args["workspace_root"], str(workspace))
        self.assertNotIn("languages", nested_args)
        self.assertNotIn("lsp_setup", nested_args)


class MinionLLMBrokerSerializationTests(unittest.TestCase):
    def test_llm_request_round_trips(self) -> None:
        request = CanonicalLLMRequest(
            messages=[{"role": "user", "content": "hi"}],
            max_output_tokens=123,
            thinking_budget_tokens=97,
            model_hint="model",
            temperature=0.2,
            tools=[{"type": "function", "function": {"name": "tool"}}],
            metadata={"run_id": "r"},
        )

        restored = llm_request_from_payload(llm_request_to_payload(request))

        self.assertEqual(restored.messages, request.messages)
        self.assertEqual(restored.max_output_tokens, 123)
        self.assertEqual(restored.thinking_budget_tokens, 97)
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
            input_tokens=41,
            uncached_input_tokens=11,
            cached_input_tokens=25,
            cache_write_input_tokens=5,
            output_tokens=13,
            reasoning_tokens=8,
            cost=0.21,
            usage_reported=True,
            provider_response_count=2,
            provider_specific_fields={"reasoning_content": "hidden"},
        )

        restored = llm_outcome_from_payload(llm_outcome_to_payload(outcome))

        self.assertEqual(restored.text, "ok")
        self.assertEqual(restored.reasoning_text, "hidden")
        self.assertEqual(restored.finish_reason, "tool_calls")
        self.assertEqual(restored.input_tokens, 41)
        self.assertEqual(restored.uncached_input_tokens, 11)
        self.assertEqual(restored.cached_input_tokens, 25)
        self.assertEqual(restored.cache_write_input_tokens, 5)
        self.assertEqual(restored.output_tokens, 13)
        self.assertEqual(restored.reasoning_tokens, 8)
        self.assertEqual(restored.cost, 0.21)
        self.assertTrue(restored.usage_reported)
        self.assertEqual(restored.provider_response_count, 2)
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
                pack = MinionInvocationPack(invocation_id="wo_broker", goal="g")
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

    def test_manager_llm_broker_records_endpoint_progress_from_host_runtime(self) -> None:
        async def scenario() -> None:
            with tempfile.TemporaryDirectory(prefix="pal_minion_broker_events_") as tmp:
                manager = MinionManager(runtime_root=Path(tmp))
                pack = MinionInvocationPack(invocation_id="wo_broker_events", goal="g")
                state = MinionRunState(minion_id="m", run_id="run_broker_events", pack=pack, status="running")
                manager.runs[state.run_id] = state
                recorded: list[dict[str, object]] = []
                manager.events.queue_event = lambda event: recorded.append(dict(event))  # type: ignore[method-assign]

                class Settings:
                    def get_think_level(self):
                        return "balanced"

                    def get_active_llm_endpoint_id(self):
                        return None

                    def set_active_llm_endpoint_id(self, endpoint_id):
                        self.active_endpoint_id = endpoint_id

                class Invoker:
                    def invoke(self, endpoint, request):
                        _ = request
                        if endpoint.endpoint_id == "broken":
                            raise RuntimeError("broken endpoint")
                        return CanonicalLLMOutcome(text=f"ok:{endpoint.endpoint_id}")

                    def invoke_stream(self, endpoint, request):
                        raise NotImplementedError

                broken = SimpleNamespace(
                    endpoint_id="broken",
                    model_id="broken-model",
                    provider="stub",
                    base_url="",
                    capabilities_blob={},
                    supports_streaming=False,
                    supports_vision=False,
                    max_output_tokens=1024,
                    context_window=8192,
                    input_modalities_blob=[],
                )
                working = SimpleNamespace(
                    endpoint_id="working",
                    model_id="working-model",
                    provider="stub",
                    base_url="",
                    capabilities_blob={},
                    supports_streaming=False,
                    supports_vision=False,
                    max_output_tokens=1024,
                    context_window=8192,
                    input_modalities_blob=[],
                )
                runtime = LLMRuntime(
                    endpoint_resolver=EndpointResolver(endpoints=(broken, working)),
                    settings_repository=Settings(),
                    endpoint_invoker=Invoker(),
                    endpoint_retry_attempts=1,
                )

                async def fake_runtime():
                    return runtime

                manager._llm_broker_runtime = fake_runtime  # type: ignore[method-assign]
                generated = await manager.llm_broker_generate(
                    {
                        "run_id": state.run_id,
                        "request": llm_request_to_payload(
                            CanonicalLLMRequest(messages=[{"role": "user", "content": "ping"}], max_output_tokens=10)
                        ),
                    }
                )
                await asyncio.sleep(0)

                self.assertEqual(generated["outcome"]["text"], "ok:working")
                endpoint_events = [
                    event
                    for event in recorded
                    if event.get("event_kind") == "progress"
                    and str(event["payload"].get("phase") or "").startswith("llm_endpoint_")
                ]
                phases = [event["payload"]["phase"] for event in endpoint_events]
                self.assertIn("llm_endpoint_attempt_failed", phases)
                self.assertIn("llm_endpoint_exhausted", phases)
                self.assertIn("llm_endpoint_fallback_succeeded", phases)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()

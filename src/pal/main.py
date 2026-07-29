from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from pal.foundation.service_logging import configure_process_logging
from pal.runtime_app import build_runtime_app
from pal.socket_client import default_socket_path, run_tty, send_message
from pal.web_fetch import run_browser_service_cli


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pal")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # -- setup ---------------------------------------------------------------
    setup_parser = subparsers.add_parser("setup", aliases=("wizard", "wizzard"), help="Interactive setup wizard")
    setup_parser.add_argument("--check", action="store_true", help="Check local runtime dependencies without provisioning")
    setup_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help="Use this runtime root instead of prompting for one",
    )
    setup_parser.set_defaults(command="setup")

    subparsers.add_parser("doctor", help="Check local Pal runtime dependencies")

    # -- run -----------------------------------------------------------------
    run_parser = subparsers.add_parser("run", help="Run the Pal runtime")
    run_parser.add_argument("--runtime-root", type=Path, required=True)

    # -- client --------------------------------------------------------------
    client_parser = subparsers.add_parser("client", help="Send one message to a running Pal instance")
    client_parser.add_argument("--runtime-root", type=Path, required=True)
    client_parser.add_argument("--message", required=True)

    tty_parser = subparsers.add_parser(
        "tty",
        help="Open an async Prompt Toolkit session with Rich Markdown output",
        description=(
            "Open an interactive TTY against a running Pal instance. Input uses "
            "Prompt Toolkit and assistant replies render as Rich Markdown. Use "
            "/exit or /quit (or Ctrl-D) to quit; Ctrl-C clears the current input."
        ),
    )
    tty_parser.add_argument("--runtime-root", type=Path, required=True)

    # -- browser-service -----------------------------------------------------
    browser_service_parser = subparsers.add_parser(
        "browser-service",
        help="Run the internal Playwright fetch browser service",
    )
    browser_service_parser.add_argument("--runtime-root", type=Path, required=True)
    browser_service_parser.add_argument("--host", required=True)
    browser_service_parser.add_argument("--port", type=int, required=True)
    browser_service_parser.add_argument("--token", required=True)
    browser_service_parser.add_argument("--idle-timeout-seconds", type=int, default=60)
    browser_service_parser.add_argument("--max-concurrency", type=int, default=2)

    # -- codex-bridge --------------------------------------------------------
    codex_bridge_parser = subparsers.add_parser(
        "codex-bridge",
        aliases=("codex-proxy",),
        help="Run a local OpenAI-compatible bridge backed by Codex CLI",
    )
    codex_bridge_parser.set_defaults(command="codex-bridge")
    codex_bridge_parser.add_argument("--host", default="127.0.0.1")
    codex_bridge_parser.add_argument("--port", type=int, default=8765)
    codex_bridge_parser.add_argument("--codex-bin", default=None)
    codex_bridge_parser.add_argument("--timeout-seconds", type=int, default=120)
    codex_bridge_parser.add_argument("--api-key-env", default="PAL_CODEX_BRIDGE_API_KEY")
    codex_bridge_parser.add_argument("--models-env", default="PAL_CODEX_BRIDGE_MODELS")
    codex_bridge_parser.add_argument("--max-concurrency", type=int, default=None)
    codex_bridge_parser.add_argument("--max-concurrency-env", default="PAL_CODEX_BRIDGE_MAX_CONCURRENCY")

    # -- eval tools ----------------------------------------------------------
    eval_parser = subparsers.add_parser("eval", help="Run versioned Pal evaluations")
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command", required=True)
    tools_eval_parser = eval_subparsers.add_parser("tools", help="Run the LLM tool-usability benchmark")
    tools_eval_parser.add_argument("--runtime-root", type=Path, required=True)
    tools_eval_parser.add_argument("--manifest", type=Path, default=None)
    tools_eval_parser.add_argument("--output", type=Path, default=None)

    return parser


async def _run_async(args: argparse.Namespace) -> int:
    if args.command == "run":
        configure_process_logging(component="pal")
        app = build_runtime_app(args.runtime_root)
        await app.run()
        return 0
    if args.command == "client":
        await send_message(default_socket_path(args.runtime_root), args.message)
        return 0
    if args.command == "tty":
        await run_tty(default_socket_path(args.runtime_root))
        return 0
    return 1


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if args.command == "doctor":
        from pal.wizard.cli import run_dependency_doctor
        return run_dependency_doctor()

    # -- setup is synchronous, no asyncio ------------------------------------
    if args.command == "setup":
        from pal.wizard.cli import run_setup_wizard
        if getattr(args, "check", False):
            from pal.wizard.cli import run_dependency_doctor
            return run_dependency_doctor()
        return run_setup_wizard(runtime_root=getattr(args, "runtime_root", None))

    if args.command == "browser-service":
        return run_browser_service_cli(
            runtime_root=args.runtime_root,
            host=str(args.host),
            port=int(args.port),
            token=str(args.token),
            idle_timeout_seconds=int(args.idle_timeout_seconds),
            max_concurrency=int(args.max_concurrency),
        )
    if args.command == "codex-bridge":
        from pal.llm.codex_openai_bridge import run_codex_openai_bridge_cli

        return run_codex_openai_bridge_cli(
            host=str(args.host),
            port=int(args.port),
            codex_bin=getattr(args, "codex_bin", None),
            timeout_seconds=int(args.timeout_seconds),
            api_key_env=str(args.api_key_env),
            models_env=str(args.models_env),
            max_concurrency=getattr(args, "max_concurrency", None),
            max_concurrency_env=str(args.max_concurrency_env),
        )
    if args.command == "eval" and args.eval_command == "tools":
        from pal.eval_tools import DEFAULT_TOOLS_BENCHMARK, run_tools_eval_cli

        return run_tools_eval_cli(
            runtime_root=args.runtime_root,
            manifest_path=args.manifest or DEFAULT_TOOLS_BENCHMARK,
            output_path=args.output,
        )
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

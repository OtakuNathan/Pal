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
        "--upgrade",
        action="store_true",
        help="Upgrade an existing runtime without interactive reconfiguration",
    )
    setup_parser.add_argument(
        "--runtime-root",
        type=Path,
        default=None,
        help="Use this runtime root instead of prompting for one",
    )
    setup_parser.set_defaults(command="setup")

    subparsers.add_parser("doctor", help="Check local Pal runtime dependencies")

    # -- llm -----------------------------------------------------------------
    llm_parser = subparsers.add_parser("llm", help="Manage configured LLM endpoints")
    from pal.llm.cli import configure_llm_parser

    configure_llm_parser(llm_parser)

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

    if args.command == "llm":
        from pal.llm.cli import run_llm_cli

        return run_llm_cli(args)

    # -- setup is synchronous, no asyncio ------------------------------------
    if args.command == "setup":
        from pal.wizard.cli import run_setup_wizard
        if getattr(args, "check", False) and getattr(args, "upgrade", False):
            parser.error("setup --check and --upgrade are mutually exclusive")
        if getattr(args, "check", False):
            from pal.wizard.cli import run_dependency_doctor
            return run_dependency_doctor()
        if getattr(args, "upgrade", False):
            from pal.wizard.cli import run_setup_upgrade
            runtime_root = getattr(args, "runtime_root", None)
            if runtime_root is None:
                parser.error("setup --upgrade requires --runtime-root")
            return run_setup_upgrade(runtime_root=runtime_root)
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

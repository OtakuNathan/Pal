from __future__ import annotations

import argparse
import asyncio
import errno
import os
import sys
from pathlib import Path

from pal.cli_paths import default_runtime_root, RUNTIME_ROOT_HELP, RUNTIME_ROOT_HINT
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
        default=default_runtime_root(),
        help=RUNTIME_ROOT_HELP,
    )
    setup_parser.set_defaults(command="setup")

    subparsers.add_parser("doctor", help="Check local Pal runtime dependencies").add_argument("--runtime-root", type=Path, default=default_runtime_root(), help=RUNTIME_ROOT_HELP)

    # -- llm -----------------------------------------------------------------
    llm_parser = subparsers.add_parser("llm", help="Manage configured LLM endpoints")
    from pal.llm.cli import configure_llm_parser

    configure_llm_parser(llm_parser)

    # -- provider ------------------------------------------------------------
    provider_parser = subparsers.add_parser("provider", help="Install channel-provider artifacts")
    from pal.provider_cli import configure_provider_parser

    configure_provider_parser(provider_parser)

    from pal.packages.cli import configure_package_parser
    configure_package_parser(subparsers.add_parser("package", help="Build and prepare plugin packages"))

    # -- run -----------------------------------------------------------------
    run_parser = subparsers.add_parser("run", help="Run the Pal runtime")
    run_parser.add_argument("--runtime-root", type=Path, default=default_runtime_root(), help=RUNTIME_ROOT_HELP)

    # -- client --------------------------------------------------------------
    client_parser = subparsers.add_parser("client", help="Send one message to a running Pal instance")
    client_parser.add_argument("--runtime-root", type=Path, default=default_runtime_root(), help=RUNTIME_ROOT_HELP)
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
    tty_parser.add_argument("--runtime-root", type=Path, default=default_runtime_root(), help=RUNTIME_ROOT_HELP)

    # -- browser-service -----------------------------------------------------
    browser_service_parser = subparsers.add_parser(
        "browser-service",
        help="Run the internal Playwright CLI browser sidecar",
    )
    browser_service_parser.add_argument("--runtime-root", type=Path, required=True)
    browser_service_parser.add_argument("--host", required=True)
    browser_service_parser.add_argument("--port", type=int, required=True)
    browser_service_parser.add_argument("--token", default="")
    browser_service_parser.add_argument("--idle-timeout-seconds", type=int, default=60)
    browser_service_parser.add_argument("--max-concurrency", type=int, default=2)

    # -- eval tools ----------------------------------------------------------
    eval_parser = subparsers.add_parser("eval", help="Run versioned Pal evaluations")
    eval_subparsers = eval_parser.add_subparsers(dest="eval_command", required=True)
    tools_eval_parser = eval_subparsers.add_parser("tools", help="Run the LLM tool-usability benchmark")
    tools_eval_parser.add_argument("--runtime-root", type=Path, default=default_runtime_root(), help=RUNTIME_ROOT_HELP)
    tools_eval_parser.add_argument("--manifest", type=Path, default=None)
    tools_eval_parser.add_argument("--output", type=Path, default=None)
    tools_eval_parser.add_argument(
        "--endpoint-id",
        default=None,
        help="Require one enabled LLM endpoint and disable endpoint fallback",
    )

    # -- bunshin --------------------------------------------------------------
    from pal.bunshin.efficiency_cli import register_bunshin_subparser

    register_bunshin_subparser(subparsers)

    return parser


async def _run_async(args: argparse.Namespace) -> int:
    if args.command == "run":
        from pal.runtime_app import DEFAULT_DB_FILENAME
        if not (args.runtime_root / DEFAULT_DB_FILENAME).is_file():
            print(f"No configured Pal runtime found at {args.runtime_root}. "
                  f"{RUNTIME_ROOT_HINT} Run pal setup for a new installation.", file=sys.stderr)
            return 2
        configure_process_logging(component="pal")
        from pal.packages.process import runtime_lease
        with runtime_lease(args.runtime_root):
            app = build_runtime_app(args.runtime_root)
            await app.run()
        return 0
    if args.command in {"client", "tty"}:
        socket = default_socket_path(args.runtime_root)
        if not socket.is_socket():
            print(f"No Pal socket found at {socket}. Start Pal for this runtime. {RUNTIME_ROOT_HINT}", file=sys.stderr)
            return 2
        try:
            if args.command == "client":
                await send_message(socket, args.message)
            else:
                if not await run_tty(socket):
                    return 2
        except OSError as exc:
            if exc.errno not in {errno.ENOENT, errno.ECONNREFUSED, errno.ENOTSOCK}:
                raise
            print(f"Cannot connect to Pal at {socket}. Start Pal for this runtime, "
                  f"{RUNTIME_ROOT_HINT}", file=sys.stderr)
            return 2
        return 0
    return 1


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if getattr(args, "runtime_root", None) is not None:
        args.runtime_root = Path(args.runtime_root).expanduser().resolve()

    requires_existing = args.command in {"llm", "eval", "bunshin"} or (
        args.command == "package" and args.package_command == "status")
    if requires_existing and not args.runtime_root.is_dir():
        print(f"No Pal runtime directory found at {args.runtime_root}. {RUNTIME_ROOT_HINT}", file=sys.stderr)
        return 2

    if args.command == "doctor":
        from pal.wizard.cli import run_dependency_doctor
        return run_dependency_doctor(runtime_root=getattr(args, "runtime_root", None))

    if args.command == "llm":
        from pal.llm.cli import run_llm_cli

        return run_llm_cli(args)

    if args.command == "provider":
        from pal.provider_cli import run_provider_cli

        return run_provider_cli(args)

    if args.command == "package":
        from pal.packages.cli import run_package_cli
        return run_package_cli(args)

    # -- setup is synchronous, no asyncio ------------------------------------
    if args.command == "setup":
        from pal.wizard.cli import run_setup_wizard
        if getattr(args, "check", False) and getattr(args, "upgrade", False):
            parser.error("setup --check and --upgrade are mutually exclusive")
        if getattr(args, "check", False):
            from pal.wizard.cli import run_dependency_doctor
            return run_dependency_doctor(runtime_root=getattr(args, "runtime_root", None))
        if getattr(args, "upgrade", False):
            from pal.wizard.cli import run_setup_upgrade
            runtime_root = getattr(args, "runtime_root", None)
            return run_setup_upgrade(runtime_root=runtime_root)
        return run_setup_wizard(runtime_root=getattr(args, "runtime_root", None))

    if args.command == "browser-service":
        browser_service_token = str(
            args.token or os.environ.get("PAL_BROWSER_SERVICE_TOKEN") or ""
        ).strip()
        if not browser_service_token:
            parser.error(
                "browser-service requires PAL_BROWSER_SERVICE_TOKEN or --token"
            )
        return run_browser_service_cli(
            runtime_root=args.runtime_root,
            host=str(args.host),
            port=int(args.port),
            token=browser_service_token,
            idle_timeout_seconds=int(args.idle_timeout_seconds),
            max_concurrency=int(args.max_concurrency),
        )
    if args.command == "eval" and args.eval_command == "tools":
        from pal.eval_tools import DEFAULT_TOOLS_BENCHMARK, run_tools_eval_cli

        return run_tools_eval_cli(
            runtime_root=args.runtime_root,
            manifest_path=args.manifest or DEFAULT_TOOLS_BENCHMARK,
            output_path=args.output,
            endpoint_id=args.endpoint_id,
        )
    if args.command == "bunshin" and args.bunshin_command == "efficiency":
        from pal.bunshin.efficiency_cli import run_efficiency_command

        return run_efficiency_command(args, stdout=sys.stdout, stderr=sys.stderr)
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

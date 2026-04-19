from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pal.execution import CapabilityCall
from pal.llm import CanonicalToolCall
from pal.runtime_app import build_runtime_app
from pal.runtime_app import open_runtime
from pal.socket_client import default_socket_path, send_message
from pal.web_fetch import run_browser_service_cli


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pal")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run the Pal runtime")
    run_parser.add_argument("--runtime-root", type=Path, required=True)
    run_parser.add_argument("--debug-prompt", action="store_true")

    client_parser = subparsers.add_parser("client", help="Send one message to a running Pal instance")
    client_parser.add_argument("--runtime-root", type=Path, required=True)
    client_parser.add_argument("--message", required=True)

    tool_call_parser = subparsers.add_parser(
        "tool-call",
        help="Simulate one canonical tool call against the execution runtime",
    )
    tool_call_parser.add_argument("--runtime-root", type=Path, required=True)
    tool_call_parser.add_argument("--name", required=True)
    tool_call_parser.add_argument("--args", default="{}")

    cap_call_parser = subparsers.add_parser(
        "cap-call",
        help="Invoke one capability directly against the execution runtime",
    )
    cap_call_parser.add_argument("--runtime-root", type=Path, required=True)
    cap_call_parser.add_argument("--name", required=True)
    cap_call_parser.add_argument("--args", default="{}")

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
    return parser


def _parse_json_object(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip() or "{}"
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"--args must be valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit("--args must decode to a JSON object")
    return payload


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _canonical_tool_result_debug_payload(result) -> dict[str, Any]:
    return {
        "name": result.name,
        "ok": bool(result.ok),
        "text": str(result.text or ""),
        "llm_text": str(result.llm_text or ""),
        "structured": result.structured,
    }


def _capability_result_debug_payload(result) -> dict[str, Any]:
    return {
        "status": result.status,
        "text": str(result.text or ""),
        "llm_text": str(result.llm_text or ""),
        "structured": result.structured,
    }


def _redirect_stdio_to_log(runtime_root: Path) -> None:
    log_path = runtime_root / "pal.log"
    log_file = open(log_path, "a", encoding="utf-8")  # noqa: SIM115
    import sys
    sys.stdout = log_file
    sys.stderr = log_file


async def _run_async(args: argparse.Namespace) -> int:
    if args.command == "run":
        _redirect_stdio_to_log(args.runtime_root)
        app = build_runtime_app(args.runtime_root, debug_prompt=True)
        await app.run()
        return 0
    if args.command == "client":
        await send_message(default_socket_path(args.runtime_root), args.message)
        return 0
    if args.command == "tool-call":
        handle = open_runtime(args.runtime_root)
        try:
            call = CanonicalToolCall(name=str(args.name), args=_parse_json_object(args.args))
            _print_json(
                {
                    "mode": "tool_call",
                    "request": {"name": call.name, "args": dict(call.args)},
                }
            )
            result = handle.core.context.execution_runtime.execute_tool(call)
            _print_json(
                {
                    "mode": "tool_call",
                    "request": {"name": call.name, "args": dict(call.args)},
                    "result": _canonical_tool_result_debug_payload(result),
                }
            )
            return 0 if result.ok else 2
        finally:
            await handle.stop_async()
    if args.command == "cap-call":
        handle = open_runtime(args.runtime_root)
        try:
            call = CapabilityCall(name=str(args.name), args=_parse_json_object(args.args))
            _print_json(
                {
                    "mode": "capability_call",
                    "request": {"name": call.name, "args": dict(call.args)},
                }
            )
            result = handle.core.context.execution_runtime.execute(call)
            _print_json(
                {
                    "mode": "capability_call",
                    "request": {"name": call.name, "args": dict(call.args)},
                    "result": _capability_result_debug_payload(result),
                }
            )
            return 0 if str(result.status).lower() == "ok" else 2
        finally:
            await handle.stop_async()
    return 1


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    if args.command == "browser-service":
        return run_browser_service_cli(
            runtime_root=args.runtime_root,
            host=str(args.host),
            port=int(args.port),
            token=str(args.token),
            idle_timeout_seconds=int(args.idle_timeout_seconds),
            max_concurrency=int(args.max_concurrency),
        )
    return asyncio.run(_run_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

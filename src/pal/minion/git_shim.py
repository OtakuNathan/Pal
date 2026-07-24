from __future__ import annotations

import os
import shlex
import socket
import sys
from pathlib import Path
from typing import Sequence
from uuid import uuid4

import msgpack


GIT_TRAP_EXIT_CODE = 126
PAL_MINION_RUNTIME_ROOT_ENV = "PAL_MINION_RUNTIME_ROOT"
ROLE_GATEWAY_TOKEN_ENV = "PAL_MINION_ROLE_ASSIGNMENT_TOKEN"
_ROLE_GATEWAY_TIMEOUT_SECONDS = 300.0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        args, effective_cwd = _normalize_read_invocation(args, cwd=Path(os.getcwd()))
    except ValueError as exc:
        print(f"blocked git command: {exc}. Leave repository mutations to the Manager.", file=sys.stderr)
        return GIT_TRAP_EXIT_CODE
    command = shlex.join(args)

    runtime_root = str(os.environ.get(PAL_MINION_RUNTIME_ROOT_ENV) or "").strip()
    if not runtime_root:
        print("read-only Git gateway is unavailable: runtime root is missing", file=sys.stderr)
        return GIT_TRAP_EXIT_CODE
    client = role_gateway_client_from_env(Path(runtime_root))
    if client is None:
        print("read-only Git gateway is unavailable: assignment token is missing", file=sys.stderr)
        return GIT_TRAP_EXIT_CODE
    try:
        response = client.request_sync(
            "git_read",
            {"cmd": command, "cwd": str(effective_cwd)},
        )
    except Exception as exc:
        print(f"read-only Git gateway failed: {exc}", file=sys.stderr)
        return GIT_TRAP_EXIT_CODE

    stdout = str(response.get("stdout") or "")
    stderr = str(response.get("stderr") or "")
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    try:
        return int(response.get("returncode", 1))
    except (TypeError, ValueError):
        return 1


def _normalize_read_invocation(args: list[str], *, cwd: Path) -> tuple[list[str], Path]:
    """Resolve Git's safe cwd selector before the command reaches the role gateway.

    The gateway validates the resulting real path against the immutable assignment
    workspace. Other global options stay in the command and are rejected by the
    shared classifier, so options such as ``-c`` or ``--git-dir`` cannot bypass
    the read-only policy.
    """

    remaining = list(args)
    effective_cwd = cwd.expanduser().resolve()
    index = 0
    while index < len(remaining):
        token = remaining[index]
        if token == "--no-pager":
            remaining.pop(index)
            continue
        if token == "-C":
            if index + 1 >= len(remaining):
                raise ValueError("git -C requires a directory")
            target = remaining[index + 1]
            del remaining[index : index + 2]
            effective_cwd = _resolve_git_cwd(effective_cwd, target)
            continue
        if token.startswith("-C") and token != "-C":
            remaining.pop(index)
            effective_cwd = _resolve_git_cwd(effective_cwd, token[2:])
            continue
        break
    return remaining, effective_cwd


def _resolve_git_cwd(current: Path, value: str) -> Path:
    target = Path(value).expanduser()
    return (target if target.is_absolute() else current / target).resolve()


class _RoleGatewayClient:
    def __init__(self, runtime_root: Path, access_token: str) -> None:
        self.runtime_root = Path(runtime_root)
        self.access_token = access_token

    def request_sync(self, method: str, params: dict[str, str]) -> dict[str, object]:
        request_id = uuid4().hex
        payload = {
            "type": "request",
            "id": request_id,
            "method": method,
            "params": {**dict(params), "access_token": self.access_token},
        }
        packed = msgpack.packb(payload, use_bin_type=True)
        with _open_role_gateway(self.runtime_root) as connection:
            connection.settimeout(_ROLE_GATEWAY_TIMEOUT_SECONDS)
            connection.sendall(len(packed).to_bytes(4, "big") + packed)
            raw_size = _recv_exact(connection, 4)
            size = int.from_bytes(raw_size, "big")
            response = msgpack.unpackb(_recv_exact(connection, size), raw=False)
        if not isinstance(response, dict) or str(response.get("id") or "") != request_id:
            raise RuntimeError("role gateway returned an invalid response")
        if not bool(response.get("ok")):
            error = dict(response.get("error") or {})
            raise RuntimeError(str(error.get("message") or "role gateway request failed"))
        result = response.get("result")
        return dict(result or {}) if isinstance(result, dict) else {}


def role_gateway_client_from_env(runtime_root: Path) -> _RoleGatewayClient | None:
    token = str(os.environ.get(ROLE_GATEWAY_TOKEN_ENV) or "").strip()
    if not token:
        return None
    return _RoleGatewayClient(runtime_root, token)


def _open_role_gateway(runtime_root: Path) -> socket.socket:
    endpoint_root = runtime_root / "data" / "minion-role"
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(_ROLE_GATEWAY_TIMEOUT_SECONDS)
    connection.connect(str(endpoint_root / "role.sock"))
    return connection


def _recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise EOFError("role gateway closed before the response was complete")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


if __name__ == "__main__":
    raise SystemExit(main())

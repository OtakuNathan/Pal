from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import msgpack


def pack_sidecar_message(payload: dict[str, Any]) -> bytes:
    packed = msgpack.packb(payload, use_bin_type=True)
    return len(packed).to_bytes(4, "big") + packed


async def read_sidecar_message(reader) -> dict[str, Any]:
    raw_size = await reader.readexactly(4)
    size = int.from_bytes(raw_size, "big")
    payload = await reader.readexactly(size)
    decoded = msgpack.unpackb(payload, raw=False)
    if not isinstance(decoded, dict):
        raise ValueError("sidecar payload must decode to an object")
    return decoded


def read_sidecar_message_sync(stream) -> dict[str, Any]:
    raw_size = stream.read(4)
    if len(raw_size or b"") != 4:
        raise EOFError("sidecar stream ended before frame size")
    size = int.from_bytes(raw_size, "big")
    payload = stream.read(size)
    if len(payload or b"") != size:
        raise EOFError("sidecar stream ended before frame payload")
    decoded = msgpack.unpackb(payload, raw=False)
    if not isinstance(decoded, dict):
        raise ValueError("sidecar payload must decode to an object")
    return decoded


class SidecarRpcError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "protocol", payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.payload = dict(payload or {})


@dataclass(frozen=True)
class SidecarEndpoint:
    runtime_root: Path
    name: str
    socket_filename: str = "manager.sock"
    port_filename: str = "manager.port"

    @property
    def runtime_dir(self) -> Path:
        return Path(self.runtime_root) / "data" / self.name

    @property
    def socket_path(self) -> Path:
        return self.runtime_dir / self.socket_filename

    @property
    def port_path(self) -> Path:
        return self.runtime_dir / self.port_filename


@dataclass
class SidecarRpcClient:
    endpoint: SidecarEndpoint
    request_timeout_seconds: float = 300.0

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = str(uuid4())
        reader, writer = await open_sidecar_connection(self.endpoint)
        try:
            writer.write(
                pack_sidecar_message(
                    {
                        "type": "request",
                        "id": request_id,
                        "method": method,
                        "params": dict(params or {}),
                    }
                )
            )
            await writer.drain()
            response = await asyncio.wait_for(read_sidecar_message(reader), timeout=self.request_timeout_seconds)
        finally:
            writer.close()
            await writer.wait_closed()
        if str(response.get("id") or "") != request_id:
            raise SidecarRpcError("sidecar returned mismatched request id", payload=response)
        if not bool(response.get("ok")):
            error = dict(response.get("error") or {})
            raise SidecarRpcError(
                str(error.get("message") or "sidecar request failed"),
                kind=str(error.get("kind") or "protocol"),
                payload=error,
            )
        result = response.get("result")
        return dict(result or {})

    def request_sync(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return run_blocking(self.request(method, params))


def run_blocking(awaitable):
    if not inspect.isawaitable(awaitable):
        return awaitable
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, awaitable).result()


async def open_sidecar_connection(endpoint: SidecarEndpoint):
    if endpoint.port_path.exists():
        port_text = endpoint.port_path.read_text(encoding="utf-8").strip()
        return await asyncio.open_connection("127.0.0.1", int(port_text))
    if hasattr(asyncio, "open_unix_connection"):
        try:
            return await asyncio.open_unix_connection(str(endpoint.socket_path))
        except (FileNotFoundError, ConnectionRefusedError, ConnectionError, OSError):
            if not endpoint.port_path.exists():
                raise
            port_text = endpoint.port_path.read_text(encoding="utf-8").strip()
            return await asyncio.open_connection("127.0.0.1", int(port_text))
    port_text = endpoint.port_path.read_text(encoding="utf-8").strip()
    return await asyncio.open_connection("127.0.0.1", int(port_text))


async def start_sidecar_server(endpoint: SidecarEndpoint, handler):
    endpoint.runtime_dir.mkdir(parents=True, exist_ok=True)
    if hasattr(asyncio, "start_unix_server"):
        path = endpoint.socket_path
        try:
            await prepare_unix_socket(path)
            server = await asyncio.start_unix_server(handler, path=str(path))
            return server, {"transport": "unix", "socket_path": str(path)}
        except (PermissionError, OSError):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
            return await _start_tcp_sidecar_server(endpoint, handler)
    return await _start_tcp_sidecar_server(endpoint, handler)


async def _start_tcp_sidecar_server(endpoint: SidecarEndpoint, handler):
    port = choose_loopback_port()
    server = await asyncio.start_server(handler, host="127.0.0.1", port=port)
    endpoint.port_path.write_text(str(port), encoding="utf-8")
    return server, {"transport": "tcp_loopback", "host": "127.0.0.1", "port": port}


async def cleanup_sidecar_endpoint(endpoint: SidecarEndpoint) -> None:
    if hasattr(asyncio, "start_unix_server"):
        path = endpoint.socket_path
        if path.exists():
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
    path = endpoint.port_path
    if path.exists():
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


async def prepare_unix_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return
    try:
        reader, writer = await asyncio.open_unix_connection(str(path))
    except (FileNotFoundError, ConnectionRefusedError, ConnectionError, OSError):
        os.unlink(path)
        return
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    raise RuntimeError(f"socket already in use: {path}")


def choose_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def python_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    src_root = Path(__file__).resolve().parents[2]
    if (src_root / "pal").exists():
        existing = [item for item in str(env.get("PYTHONPATH") or "").split(os.pathsep) if item]
        src_text = str(src_root)
        if src_text not in existing:
            env["PYTHONPATH"] = os.pathsep.join([src_text, *existing])
    return env


async def handle_sidecar_client(reader, writer, dispatch: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]) -> None:
    try:
        while True:
            try:
                request = await read_sidecar_message(reader)
            except asyncio.IncompleteReadError:
                return
            response = await dispatch(request)
            writer.write(pack_sidecar_message(response))
            await writer.drain()
    except (ConnectionError, OSError, ValueError):
        return
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def dispatch_sidecar_request(
    request: dict[str, Any],
    call_method: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]],
    *,
    error_kind: Callable[[Exception], str] | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    request_id = str(request.get("id") or "")
    method = str(request.get("method") or "")
    params = dict(request.get("params") or {})
    try:
        result = await call_method(method, params)
        return {"type": "response", "id": request_id, "ok": True, "result": result}
    except Exception as exc:
        if logger is not None:
            logger.exception("sidecar request failed: %s", method)
        kind = error_kind(exc) if error_kind is not None else "sidecar"
        return {
            "type": "response",
            "id": request_id,
            "ok": False,
            "error": {"kind": kind, "message": f"{exc.__class__.__name__}: {exc}"},
        }

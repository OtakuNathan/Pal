from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import socket
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import msgpack


MAX_SIDECAR_FRAME_BYTES = 16 * 1024 * 1024
SIDECAR_READ_CHUNK_BYTES = 64 * 1024


def pack_sidecar_message(payload: dict[str, Any]) -> bytes:
    packed = msgpack.packb(payload, use_bin_type=True)
    if len(packed) > MAX_SIDECAR_FRAME_BYTES:
        raise ValueError("sidecar payload exceeds the 16 MiB frame limit")
    return len(packed).to_bytes(4, "big") + packed


async def read_sidecar_message(reader) -> dict[str, Any]:
    raw_size = await reader.readexactly(4)
    size = int.from_bytes(raw_size, "big")
    _validate_sidecar_frame_size(size)
    payload = await _read_sidecar_payload(reader, size)
    decoded = msgpack.unpackb(payload, raw=False)
    if not isinstance(decoded, dict):
        raise ValueError("sidecar payload must decode to an object")
    return decoded


def read_sidecar_message_sync(stream) -> dict[str, Any]:
    raw_size = stream.read(4)
    if len(raw_size or b"") != 4:
        raise EOFError("sidecar stream ended before frame size")
    size = int.from_bytes(raw_size, "big")
    _validate_sidecar_frame_size(size)
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(min(remaining, SIDECAR_READ_CHUNK_BYTES))
        if not chunk:
            raise EOFError("sidecar stream ended before frame payload")
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    decoded = msgpack.unpackb(payload, raw=False)
    if not isinstance(decoded, dict):
        raise ValueError("sidecar payload must decode to an object")
    return decoded


def _validate_sidecar_frame_size(size: int) -> None:
    if size < 0 or size > MAX_SIDECAR_FRAME_BYTES:
        raise ValueError("sidecar frame exceeds the 16 MiB limit")


async def _read_sidecar_payload(reader: Any, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunks.append(
            await reader.readexactly(min(remaining, SIDECAR_READ_CHUNK_BYTES))
        )
        remaining -= len(chunks[-1])
        await asyncio.sleep(0)
    return b"".join(chunks)


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
    runtime_dir_override: Path | None = None

    @property
    def runtime_dir(self) -> Path:
        if self.runtime_dir_override is not None:
            return Path(self.runtime_dir_override)
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
    unix_only: bool = False

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        timeout = max(float(self.request_timeout_seconds), 0.001)
        try:
            return await asyncio.wait_for(self._request_once(method, params), timeout=timeout)
        except TimeoutError as exc:
            raise SidecarRpcError(
                f"sidecar request timed out after {timeout:.3g}s",
                kind="timeout",
                payload={"method": method, "timeout_seconds": timeout},
            ) from exc

    async def stream(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield one result per sidecar frame until the stream terminates.

        The timeout is an idle timeout for each frame, rather than a deadline for
        the whole stream. Closing or cancelling the iterator closes the owned
        sidecar connection.
        """

        timeout = max(float(self.request_timeout_seconds), 0.001)
        request_id = str(uuid4())
        if self.unix_only:
            reader, writer = await open_sidecar_connection(
                self.endpoint,
                unix_only=True,
            )
        else:
            reader, writer = await open_sidecar_connection(self.endpoint)
        try:
            writer.write(
                pack_sidecar_message(
                    {
                        "type": "request",
                        "id": request_id,
                        "method": method,
                        "params": dict(params or {}),
                        "stream": True,
                    }
                )
            )
            await writer.drain()
            while True:
                try:
                    response = await asyncio.wait_for(
                        read_sidecar_message(reader),
                        timeout=timeout,
                    )
                except TimeoutError as exc:
                    raise SidecarRpcError(
                        f"sidecar stream was idle for {timeout:.3g}s",
                        kind="timeout",
                        payload={"method": method, "timeout_seconds": timeout},
                    ) from exc
                if str(response.get("id") or "") != request_id:
                    raise SidecarRpcError(
                        "sidecar returned mismatched stream request id",
                        payload=response,
                    )
                frame_type = str(response.get("type") or "")
                if frame_type == "stream_item":
                    if not bool(response.get("ok")):
                        raise _sidecar_error_from_response(response)
                    yield dict(response.get("result") or {})
                    continue
                if frame_type == "stream_end":
                    if not bool(response.get("ok")):
                        raise _sidecar_error_from_response(response)
                    return
                raise SidecarRpcError(
                    f"sidecar returned unexpected stream frame: {frame_type or '<missing>'}",
                    payload=response,
                )
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _request_once(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = str(uuid4())
        if self.unix_only:
            reader, writer = await open_sidecar_connection(
                self.endpoint,
                unix_only=True,
            )
        else:
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
            response = await read_sidecar_message(reader)
        finally:
            writer.close()
            await writer.wait_closed()
        if str(response.get("id") or "") != request_id:
            raise SidecarRpcError("sidecar returned mismatched request id", payload=response)
        if not bool(response.get("ok")):
            raise _sidecar_error_from_response(response)
        result = response.get("result")
        return dict(result or {})

    def request_sync(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return run_blocking(self.request(method, params))

    def stream_sync(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> "SidecarSyncStream":
        return SidecarSyncStream(
            endpoint=self.endpoint,
            method=method,
            params=dict(params or {}),
            timeout_seconds=self.request_timeout_seconds,
            unix_only=self.unix_only,
        )


class SidecarSyncStream:
    """Blocking stream iterator whose socket can be closed from another thread."""

    def __init__(
        self,
        *,
        endpoint: SidecarEndpoint,
        method: str,
        params: dict[str, Any],
        timeout_seconds: float,
        unix_only: bool = False,
    ) -> None:
        self._request_id = str(uuid4())
        self._method = str(method)
        self._socket = _open_sidecar_socket_sync(endpoint, unix_only=unix_only)
        self._socket.settimeout(max(float(timeout_seconds), 0.001))
        self._stream = self._socket.makefile("rb")
        self._closed = False
        self._lock = threading.RLock()
        self._socket.sendall(
            pack_sidecar_message(
                {
                    "type": "request",
                    "id": self._request_id,
                    "method": self._method,
                    "params": dict(params),
                    "stream": True,
                }
            )
        )

    def __iter__(self) -> "SidecarSyncStream":
        return self

    def __next__(self) -> dict[str, Any]:
        with self._lock:
            if self._closed:
                raise StopIteration
            stream = self._stream
        try:
            response = read_sidecar_message_sync(stream)
        except (EOFError, OSError, ValueError, socket.timeout) as exc:
            self.close()
            raise SidecarRpcError(
                f"sidecar stream failed while waiting for {self._method}: {exc}",
                kind="timeout" if isinstance(exc, socket.timeout) else "transport",
                payload={"method": self._method},
            ) from exc
        if str(response.get("id") or "") != self._request_id:
            self.close()
            raise SidecarRpcError(
                "sidecar returned mismatched stream request id",
                payload=response,
            )
        frame_type = str(response.get("type") or "")
        if frame_type == "stream_item":
            if not bool(response.get("ok")):
                self.close()
                raise _sidecar_error_from_response(response)
            return dict(response.get("result") or {})
        if frame_type == "stream_end":
            self.close()
            if not bool(response.get("ok")):
                raise _sidecar_error_from_response(response)
            raise StopIteration
        self.close()
        raise SidecarRpcError(
            f"sidecar returned unexpected stream frame: {frame_type or '<missing>'}",
            payload=response,
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stream = self._stream
            sock = self._socket
        with contextlib.suppress(Exception):
            sock.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(Exception):
            stream.close()
        with contextlib.suppress(Exception):
            sock.close()


def _sidecar_error_from_response(response: dict[str, Any]) -> SidecarRpcError:
    error = dict(response.get("error") or {})
    return SidecarRpcError(
        str(error.get("message") or "sidecar request failed"),
        kind=str(error.get("kind") or "protocol"),
        payload=error,
    )


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


def _open_sidecar_socket_sync(
    endpoint: SidecarEndpoint,
    *,
    unix_only: bool = False,
) -> socket.socket:
    if not unix_only and endpoint.port_path.exists():
        port_text = endpoint.port_path.read_text(encoding="utf-8").strip()
        return socket.create_connection(("127.0.0.1", int(port_text)))
    if not hasattr(socket, "AF_UNIX"):
        raise SidecarRpcError(
            "Unix sidecar transport is unavailable",
            kind="transport",
        )
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(str(endpoint.socket_path))
    except Exception:
        sock.close()
        raise
    return sock


async def open_sidecar_connection(
    endpoint: SidecarEndpoint,
    *,
    unix_only: bool = False,
):
    if unix_only:
        if not hasattr(asyncio, "open_unix_connection"):
            raise SidecarRpcError(
                "Unix sidecar transport is unavailable",
                kind="transport",
            )
        return await asyncio.open_unix_connection(str(endpoint.socket_path))
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
    node_bins = _node_bin_path_candidates(Path.home())
    if node_bins:
        existing_path = [item for item in str(env.get("PATH") or "").split(os.pathsep) if item]
        entries: list[str] = []
        seen: set[str] = set()
        for item in [*(str(path) for path in node_bins), *existing_path]:
            if item in seen:
                continue
            seen.add(item)
            entries.append(item)
        env["PATH"] = os.pathsep.join(entries)
    return env


def _node_bin_path_candidates(home: Path) -> list[Path]:
    candidates: list[Path] = []
    nvm_root = Path(home) / ".nvm" / "versions" / "node"
    if nvm_root.exists():
        candidates.extend(sorted((path / "bin" for path in nvm_root.iterdir()), key=_node_bin_sort_key, reverse=True))
    return [path for path in candidates if path.is_dir()]


def _node_bin_sort_key(path: Path) -> tuple[int, ...]:
    text = path.parent.name.lstrip("v")
    parts: list[int] = []
    for item in text.split("."):
        try:
            parts.append(int(item))
        except ValueError:
            parts.append(0)
    return tuple(parts)


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

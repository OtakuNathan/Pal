"""Atomic updates of named files through a private mount of their shared parent."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from typing import Any

from pal.execution.file_state import FileContentChangedError, atomic_compare_and_swap_utf8
from pal.execution.tool_facade import EffectReceipt, RetryDirective, ToolExecutionError
from pal.foundation.bwrap_companion import CHANNEL_ENV, shadow_file_mounts, with_companion
from pal.foundation.bwrap_permissions import append_bind_path
from pal.foundation.sidecar import pack_sidecar_message, read_sidecar_message_sync


_REQUEST_TIMEOUT_SECONDS = 300


def with_mounted_file_editor(
    worker: list[str], editor: list[str], env: dict[str, str],
    *, files: dict[str, Path], python_paths: list[str], shadow_root: Path,
) -> tuple[list[str], dict[str, str]]:
    """Give only the companion the mounts needed for atomic file updates."""
    mounts, aliases = shadow_file_mounts(files, shadow_root)
    for path in python_paths:
        append_bind_path(editor, Path(path), read_only=True)
    editor.extend([
        "--setenv", "PYTHONPATH", os.pathsep.join(python_paths), "--chdir", "/", "--",
        str(Path(sys.executable).resolve()), "-P", "-s", "-B",
        "-m", "pal.execution.mounted_file_editor", json.dumps(aliases),
    ])
    return with_companion(worker, editor, env, mounts)


def commit_mounted_file(key: str, **fields: Any) -> None:
    """One response channel per save, including when several tools run concurrently."""
    descriptor = os.environ.get(CHANNEL_ENV)
    if descriptor is None:
        raise OSError("the private file editor is unavailable")
    request = pack_sidecar_message({"path": key, **fields})
    client, peer = socket.socketpair()
    with client, peer:
        client.settimeout(_REQUEST_TIMEOUT_SECONDS)
        with socket.socket(fileno=os.dup(int(descriptor))) as channel:
            socket.send_fds(channel, [b"+"], [peer.fileno()])
        peer.close()
        client.sendall(request)
        with client.makefile("rb") as stream:
            try:
                response = read_sidecar_message_sync(stream)
            except EOFError as exc:
                raise OSError("file editor disconnected before confirming the change") from exc
    if "conflict" in response:
        raise FileContentChangedError(response["conflict"])
    if "effect" in response:
        raise ToolExecutionError(response["error"], error_code=response["code"],
                                 retry=RetryDirective.DO_NOT_RETRY,
                                 effect_receipt=EffectReceipt.model_validate(response["effect"]),
                                 details=response["details"], recovery_hint=response["hint"])
    if response.get("committed") is not True:
        raise OSError(response.get("error", "file editor did not confirm the change"))


def _commit(files: dict[str, str], request: dict[str, Any]) -> dict[str, Any]:
    key = request["path"]
    if key not in files:
        raise PermissionError("file is outside the assigned write scope")
    target = Path(files[key])
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise PermissionError("assigned path is no longer a regular file")
    try:
        if "delete_digest" in request:
            if hashlib.sha256(target.read_bytes()).hexdigest() != request["delete_digest"]:
                raise OSError("file changed before deletion")
            target.unlink()
        else:
            atomic_compare_and_swap_utf8(target, expected_content=request["expected_content"],
                                         new_content=request["new_content"])
    except FileContentChangedError as exc:
        return {"conflict": str(exc).replace(str(target), key)}
    except ToolExecutionError as exc:
        if exc.effect_receipt is None:
            raise
        return {"error": str(exc).replace(str(target), key), "code": exc.error_code,
                "effect": exc.effect_receipt.model_dump(mode="json"), "hint": exc.recovery_hint,
                "details": {**exc.details, "file_path": key}}
    return {"committed": True}


def serve(files: dict[str, str]) -> None:
    with socket.socket(fileno=0) as listener:
        while True:
            message, descriptors, flags, _ = socket.recv_fds(listener, 1, 1)
            if not message:
                return
            try:
                if message != b"+" or len(descriptors) != 1 or flags & socket.MSG_CTRUNC:
                    continue
                with socket.fromfd(descriptors[0], socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                    peer.settimeout(_REQUEST_TIMEOUT_SECONDS)
                    with peer.makefile("rb") as stream:
                        try:
                            result = _commit(files, read_sidecar_message_sync(stream))
                        except Exception as exc:
                            result = {"error": str(exc)}
                    peer.sendall(pack_sidecar_message(result))
            except OSError:
                # A caller can disappear before it receives confirmation.
                continue
            finally:
                for descriptor in descriptors:
                    os.close(descriptor)


if __name__ == "__main__":
    serve(json.loads(sys.argv[1]))

from __future__ import annotations

from typing import Any

from pal.foundation.sidecar import pack_sidecar_message, read_sidecar_message


DEFAULT_SOCKET_FILENAME = "pal.sock"


def pack_socket_message(payload: dict[str, Any]) -> bytes:
    return pack_sidecar_message(payload)


async def read_socket_message(reader) -> dict[str, Any]:
    return await read_sidecar_message(reader)

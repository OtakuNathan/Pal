from __future__ import annotations

from typing import Any

import msgpack


DEFAULT_SOCKET_FILENAME = "pal.sock"


def pack_socket_message(payload: dict[str, Any]) -> bytes:
    packed = msgpack.packb(payload, use_bin_type=True)
    return len(packed).to_bytes(4, "big") + packed


async def read_socket_message(reader) -> dict[str, Any]:
    raw_size = await reader.readexactly(4)
    size = int.from_bytes(raw_size, "big")
    payload = await reader.readexactly(size)
    decoded = msgpack.unpackb(payload, raw=False)
    if not isinstance(decoded, dict):
        raise ValueError("socket payload must decode to an object")
    return decoded

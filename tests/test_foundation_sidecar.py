from __future__ import annotations

import asyncio
import io
import unittest

from pal.foundation.sidecar import (
    MAX_SIDECAR_FRAME_BYTES,
    pack_sidecar_message,
    read_sidecar_message,
    read_sidecar_message_sync,
)


class _AsyncReader:
    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)
        self.read_sizes: list[int] = []

    async def readexactly(self, size: int) -> bytes:
        self.read_sizes.append(size)
        value = self._stream.read(size)
        if len(value) != size:
            raise asyncio.IncompleteReadError(value, size)
        return value


class FoundationSidecarTests(unittest.TestCase):
    def test_async_decoder_reads_large_frames_incrementally(self) -> None:
        packed = pack_sidecar_message({"payload": "x" * 200_000})
        reader = _AsyncReader(packed)
        decoded = asyncio.run(read_sidecar_message(reader))
        self.assertEqual(len(decoded["payload"]), 200_000)
        self.assertEqual(reader.read_sizes[0], 4)
        self.assertGreater(len(reader.read_sizes), 4)
        self.assertLessEqual(max(reader.read_sizes[1:]), 64 * 1024)

    def test_decoders_reject_oversized_frame_before_reading_payload(self) -> None:
        header = (MAX_SIDECAR_FRAME_BYTES + 1).to_bytes(4, "big")
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            read_sidecar_message_sync(io.BytesIO(header))
        reader = _AsyncReader(header)
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            asyncio.run(read_sidecar_message(reader))
        self.assertEqual(reader.read_sizes, [4])


if __name__ == "__main__":
    unittest.main()

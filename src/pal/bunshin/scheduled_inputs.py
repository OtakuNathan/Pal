"""Capture stable read-only archive inputs for a scheduled workflow."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile


def capture_conversation_archive(runtime_root: Path, workflow_id: str) -> str:
    source = runtime_root / "data" / "conversation-history"
    target = runtime_root / "data" / "bunshin" / "scheduled-inputs" / workflow_id / "conversation-history"
    if target.is_dir():
        return str(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".archive-", dir=target.parent) as staging:
        snapshot = Path(staging) / "conversation-history"
        snapshot.mkdir()
        for path in sorted(source.glob("*.jsonl")):
            if path.is_symlink() or not path.is_file():
                continue
            # Capture only the observed high-water mark. An incomplete trailing
            # record can be added by the Memory owner in a later occurrence.
            with path.open("rb") as stream:
                data = stream.read(os.fstat(stream.fileno()).st_size)
            end = data.rfind(b"\n")
            with (snapshot / path.name).open("wb") as stream:
                stream.write(data[:end + 1])
                stream.flush()
                os.fsync(stream.fileno())
        os.replace(snapshot, target)
    return str(target)

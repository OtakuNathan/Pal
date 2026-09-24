"""Explicit references to immutable tool-output copies, never file-read authority."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping


@dataclass(frozen=True)
class ResultSnapshotRef:
    snapshot_id: str
    path: str
    digest: str
    size_bytes: int
    origin_call_id: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping) -> ResultSnapshotRef:
        return cls(snapshot_id=str(value["snapshot_id"]), path=str(value["path"]),
                   digest=str(value["digest"]), size_bytes=int(value["size_bytes"]),
                   origin_call_id=str(value.get("origin_call_id", "")))


def message_snapshot_refs(message) -> tuple[ResultSnapshotRef, ...]:
    refs = list(getattr(message, "snapshot_refs", ()))
    for part in getattr(message, "parts", ()):
        refs.extend(getattr(part, "snapshot_refs", ()))
    # Only this explicit IR metadata field is a reference. Paths in arbitrary
    # text, structured results, or quoted source material never acquire leases.
    for item in getattr(message, "metadata", {}).get("result_snapshots", ()):
        refs.append(ResultSnapshotRef.from_dict(item))
    return tuple({ref.snapshot_id: ref for ref in refs}.values())


def turn_snapshot_refs(turn) -> tuple[ResultSnapshotRef, ...]:
    return tuple({ref.snapshot_id: ref for message in turn.messages
                  for ref in message_snapshot_refs(message)}.values())

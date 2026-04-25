from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttachmentSpec:
    path: str
    caption: str = ""
    file_name: str = ""
    mime_type: str = ""

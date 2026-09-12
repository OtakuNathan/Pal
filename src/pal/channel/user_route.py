"""The last accepted user route, independent of memory generations."""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from pal.control import ControlRoute


def save_user_route(path: Path, route: ControlRoute) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(asdict(route), handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_user_route(path: Path) -> ControlRoute | None:
    if not path.is_file():
        return None
    return ControlRoute(**json.loads(path.read_text(encoding="utf-8")))

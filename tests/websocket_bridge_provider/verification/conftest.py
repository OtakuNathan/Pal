"""Verification test bootstrapping for the websocket_bridge provider.

Mirrors the developer conftest: pin import resolution to this worktree's own
``src`` tree so the runtime-root provider module under test is the one authored
here, not any editable/other checkout of ``pal`` present on the interpreter path.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parents[3]
_SRC = _WORKSPACE / "src"
_PROVIDERS = _WORKSPACE / "providers"

for _name in list(sys.modules):
    if _name == "pal" or _name.startswith("pal."):
        _mod = sys.modules.get(_name)
        _file = getattr(_mod, "__file__", "") or ""
        if _file and _SRC.resolve() not in {Path(_file).resolve(), *Path(_file).resolve().parents}:
            sys.modules.pop(_name, None)

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_PROVIDERS) not in sys.path:
    sys.path.insert(0, str(_PROVIDERS))

"""Developer test bootstrapping for the websocket_bridge provider.

These tests live under ``tests/websocket_bridge_provider/developer`` and must
exercise the worktree's own source tree rather than any editable/other checkout
of ``pal`` that may be present on the interpreter path. The conftest pins the
import resolution to this worktree's ``src`` so the runtime-root provider module
under test is the one authored here.
"""

from __future__ import annotations

import sys
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parents[3]
_SRC = _WORKSPACE / "src"
_PROVIDERS = _WORKSPACE / "providers"

# Drop any ``pal`` / ``pal.*`` modules already imported from a different source
# root so that the first ``import pal`` in the test modules resolves to this
# worktree.
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

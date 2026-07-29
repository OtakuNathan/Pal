"""Load the WebSocket bridge exclusively from its external provider source."""

from __future__ import annotations

import sys
from pathlib import Path


_WORKSPACE = Path(__file__).resolve().parents[2]
_SRC = _WORKSPACE / "src"
_PROVIDERS = _WORKSPACE / "providers"

if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_PROVIDERS) not in sys.path:
    sys.path.insert(0, str(_PROVIDERS))

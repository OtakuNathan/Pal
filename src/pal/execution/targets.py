"""Shared strict tool input shape; remote plugins own alias resolution."""
from typing import Annotated
from pydantic import Field

ExecutionTarget = Annotated[int, Field(strict=True, ge=0)] | Annotated[str, Field(strict=True, min_length=1)]
TARGET_DESCRIPTION = ('0 selects the local host. Remote targets accept a configured integer ID '
    'or an exact name/shortcut, resolved by the remote plugin. Numeric strings are aliases, '
    'not integer IDs. Paths belong to the selected target.')

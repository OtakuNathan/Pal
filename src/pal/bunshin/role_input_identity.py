"""Pure semantic role-input identity shared by prompt binding and draft recovery."""
from __future__ import annotations

from typing import Any, Mapping


EPHEMERAL_ROLE_INPUT_NAMES = frozenset({"workspace_preparation"})


def _role_input_is_semantic(name: str, *, role: str, mode: str = "") -> bool:
    # Preparation describes one attempt's paths, scan counts and optional LSP
    # observations. Derived verification policy remains a semantic input.
    return name not in EPHEMERAL_ROLE_INPUT_NAMES


def _semantic_role_input_refs(
    input_refs: Mapping[str, Mapping[str, Any]], *, role: str = "", mode: str = "",
) -> dict[str, dict[str, Any]]:
    if not isinstance(input_refs, Mapping):
        raise ValueError("role input references must be an object")
    for name, reference in input_refs.items():
        if not isinstance(name, str) or not name or not isinstance(reference, Mapping):
            raise ValueError("role input references require non-empty names and object values")
    return {
        name: dict(reference) for name, reference in sorted(input_refs.items())
        if _role_input_is_semantic(name, role=role, mode=mode)
    }

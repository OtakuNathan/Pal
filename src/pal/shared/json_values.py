from __future__ import annotations

from types import MappingProxyType
from typing import Any, Mapping


def freeze_json_mapping(value: Mapping[Any, Any]) -> Mapping[Any, Any]:
    """Return an immutable projection of a JSON-like mapping."""

    return MappingProxyType(
        {key: _freeze_value(item) for key, item in value.items()}
    )


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return freeze_json_mapping(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_value(item) for item in value)
    return value


def thaw_json(value: Any) -> Any:
    """Return a mutable, SDK/JSON-serializer-safe projection."""

    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, frozenset, set)):
        return [thaw_json(item) for item in value]
    return value


__all__ = ["freeze_json_mapping", "thaw_json"]

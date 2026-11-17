"""Module-owned asynchronous configuration RPC operations."""
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

ConfigurationHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ConfigurationOperations = Mapping[str, tuple[str, ConfigurationHandler]]

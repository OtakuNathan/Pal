from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from types import ModuleType

from pal.channel.provider_manager import _load_source_module


@lru_cache(maxsize=None)
def load_runtime_channel_provider_module(
    provider_id: str,
    module_name: str,
) -> ModuleType:
    provider_root = Path(__file__).resolve().parents[1] / "providers" / provider_id
    return _load_source_module(
        f"_pal_test_channel_provider_{provider_id}.{module_name}",
        provider_root / f"{module_name}.py",
    )


def telegram_endpoint_module() -> ModuleType:
    return load_runtime_channel_provider_module("telegram", "endpoint")

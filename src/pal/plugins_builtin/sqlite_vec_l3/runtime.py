from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pal.core.runtime_config import RuntimeConfig
from pal.memory.embedding import build_ollama_embedding_provider_from_config
from pal.memory.service import MemoryService
from pal.plugins.contracts import PluginBuildContext
from pal.plugins.l3 import SQLiteVecL3Plugin, register_with_core as register_l3_with_core


@dataclass
class SQLiteVecL3BuiltinBundle:
    memory_service: MemoryService
    runtime_root: Path | None = None
    plugin_id: str = "sqlite_vec_l3"
    version: str = "0.1.0"

    def register_with_core(self, context):
        config = RuntimeConfig.load(self.runtime_root) if self.runtime_root is not None else RuntimeConfig.defaults()
        plugin = SQLiteVecL3Plugin(
            service=self.memory_service,
            embedding_provider=build_ollama_embedding_provider_from_config(config),
        )
        self.memory_service.l3_selector.active_provider_id = plugin.provider_id
        return register_l3_with_core(context, plugin)


def build_plugin(*, memory_service: MemoryService, context: PluginBuildContext | None = None) -> SQLiteVecL3BuiltinBundle:
    runtime_root = context.runtime_root if context is not None else None
    return SQLiteVecL3BuiltinBundle(memory_service=memory_service, runtime_root=runtime_root)

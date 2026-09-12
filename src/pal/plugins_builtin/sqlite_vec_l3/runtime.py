from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pal.core.runtime_config import RuntimeConfig
from pal.memory.embedding import build_ollama_embedding_provider_from_config
from pal.memory.service import MemoryService
from pal.memory.storage import MemoryStorage
from pal.plugins.contracts import PluginBuildContext
from pal.plugins.l3 import SQLiteVecL3Plugin, register_with_core as register_l3_with_core


@dataclass
class SQLiteVecL3BuiltinBundle:
    runtime_root: Path | None = None
    plugin_id: str = "sqlite_vec_l3"
    version: str = "0.1.0"

    def start(self, scope):
        memory_service: MemoryService = scope.core_context.require_port("memory:memory")
        config = RuntimeConfig.load(self.runtime_root) if self.runtime_root is not None else RuntimeConfig.defaults()
        repository_args = {}
        if self.runtime_root is not None:
            storage = MemoryStorage(self.runtime_root)
            legacy = self.runtime_root / "pal.sqlite3"
            storage.migrate(legacy) if legacy.exists() else storage.create_initial()
            from pal.bunshin.memory_binding import initialize_existing_workflow_pins
            initialize_existing_workflow_pins(self.runtime_root, storage)
            repository_args["repository"] = storage.open()
        plugin = SQLiteVecL3Plugin(
            service=memory_service,
            embedding_provider=build_ollama_embedding_provider_from_config(config),
            **repository_args,
        )
        if repository_args:
            scope.defer(lambda: plugin.repository.close())
        previous_provider_id = memory_service.l3_selector.active_provider_id
        memory_service.l3_selector.active_provider_id = plugin.provider_id
        scope.defer(lambda: setattr(memory_service.l3_selector, "active_provider_id", previous_provider_id))
        return register_l3_with_core(scope.context, plugin)


def build_plugin(*, context: PluginBuildContext | None = None) -> SQLiteVecL3BuiltinBundle:
    runtime_root = context.runtime_root if context is not None else None
    return SQLiteVecL3BuiltinBundle(runtime_root=runtime_root)

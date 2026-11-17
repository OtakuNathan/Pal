from __future__ import annotations

from dataclasses import dataclass
from contextlib import ExitStack
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
        with ExitStack() as cleanup:
            return self._start(scope, cleanup)

    def _start(self, scope, cleanup):
        memory_service: MemoryService = scope.core_context.require_port("memory:memory")
        config = RuntimeConfig.load(self.runtime_root) if self.runtime_root is not None else RuntimeConfig.defaults()
        repository_args = {}
        if self.runtime_root is not None:
            storage = MemoryStorage(self.runtime_root)
            legacy = self.runtime_root / "pal.sqlite3"
            storage.migrate(legacy) if legacy.exists() else storage.create_initial()
            storage.recover_abandoned_fence()
            repository_args["repository"] = storage.open()
            cleanup.callback(repository_args["repository"].close)
        plugin = SQLiteVecL3Plugin(
            service=memory_service,
            embedding_provider=build_ollama_embedding_provider_from_config(config),
            **repository_args,
        )
        if repository_args:
            scope.defer(plugin.close)
        cleanup.pop_all()
        return register_l3_with_core(scope.context, plugin)


def build_plugin(context: PluginBuildContext) -> SQLiteVecL3BuiltinBundle:
    runtime_root = context.runtime_root
    return SQLiteVecL3BuiltinBundle(runtime_root=runtime_root)

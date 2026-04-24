from __future__ import annotations

from dataclasses import dataclass

from pal.memory.service import MemoryService
from pal.plugins.l3 import SQLiteVecL3Plugin, register_with_core as register_l3_with_core


@dataclass
class SQLiteVecL3BuiltinBundle:
    memory_service: MemoryService
    plugin_id: str = "sqlite_vec_l3"
    version: str = "0.1.0"

    def register_with_core(self, context):
        plugin = SQLiteVecL3Plugin(service=self.memory_service)
        self.memory_service.l3_selector.active_provider_id = plugin.provider_id
        return register_l3_with_core(context, plugin)


def build_plugin(*, memory_service: MemoryService) -> SQLiteVecL3BuiltinBundle:
    return SQLiteVecL3BuiltinBundle(memory_service=memory_service)

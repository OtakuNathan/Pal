"""Build a role's read-only, generation-pinned L3 runtime."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pal.memory import MemoryService
from pal.memory.embedding_config import embedding_from_configuration
from pal.memory.storage import MemoryStorage
from pal.plugins.l3 import SQLiteVecL3Plugin


@dataclass
class RoleMemoryRuntime:
    provider: SQLiteVecL3Plugin

    def close(self) -> None:
        self.provider.close()


def build_role_memory(runtime_root: Path, *, service: MemoryService, workflow_id: str) -> RoleMemoryRuntime:
    storage = MemoryStorage(runtime_root, read_only=True)
    repository = storage.open(storage.pinned(workflow_id), read_only=True)
    try:
        row = repository.database.execute_sql(
            "SELECT configuration_json FROM memory_embedding_configuration WHERE singleton=1").fetchone()
        if row is None:
            raise RuntimeError("Pinned memory generation has no embedding configuration")
        embedding = embedding_from_configuration(json.loads(row[0]))
        provider = SQLiteVecL3Plugin(service=service, repository=repository,
            provider_id=storage.provider_for_generation(repository.generation_id),
            embedding_provider=embedding, read_only=True)
        return RoleMemoryRuntime(provider)
    except BaseException:
        repository.close()
        raise

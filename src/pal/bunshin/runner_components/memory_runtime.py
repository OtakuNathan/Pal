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
        has_configuration = repository.database.execute_sql(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='memory_embedding_configuration'").fetchone()
        row = (repository.database.execute_sql(
            "SELECT configuration_json FROM memory_embedding_configuration WHERE singleton=1").fetchone()
            if has_configuration else None)
        # Retained pre-upgrade generations have no constructor metadata. Preserve
        # their exact/lexical/topic reads without guessing a vector transport or
        # writing provenance into the workflow's pinned database. Present but
        # invalid configuration still fails rather than hiding corruption.
        embedding = embedding_from_configuration(json.loads(row[0])) if row is not None else None
        provider = SQLiteVecL3Plugin(service=service, repository=repository,
            provider_id=storage.provider_for_generation(repository.generation_id),
            embedding_provider=embedding, read_only=True, use_default_embedding=False)
        return RoleMemoryRuntime(provider)
    except BaseException:
        repository.close()
        raise

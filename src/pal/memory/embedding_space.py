"""A generation's embedding identity and vector shape are immutable."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum


class EmbeddingAccess(StrEnum):
    BLOCKED = "blocked"
    READ = "read"
    WRITE = "write"


def embedding_access(known: bool, matches: bool, writable: bool) -> EmbeddingAccess:
    if not known or not matches:
        return EmbeddingAccess.BLOCKED
    return EmbeddingAccess.WRITE if writable else EmbeddingAccess.READ


def accepts_vector(identity_matches: bool, expected_dimension: int | None, dimension: int) -> bool:
    return identity_matches and dimension > 0 and expected_dimension in (None, dimension)


@dataclass(frozen=True)
class EmbeddingSpace:
    profile: dict[str, str | None]
    dimension: int | None


def read_space(connection: sqlite3.Connection) -> EmbeddingSpace | None:
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='memory_embedding_space'").fetchone():
        return None
    row = connection.execute("SELECT profile_json,dimension FROM memory_embedding_space WHERE singleton=1").fetchone()
    return EmbeddingSpace(json.loads(row[0]), row[1]) if row else None


def claim_empty_space(connection: sqlite3.Connection, profile: dict[str, str | None]) -> EmbeddingSpace | None:
    existing = read_space(connection)
    if existing is not None:
        return existing
    if connection.execute("SELECT 1 FROM memory_embeddings LIMIT 1").fetchone() or connection.execute(
            "SELECT 1 FROM memory_embedding_vec LIMIT 1").fetchone():
        return None  # Existing vectors without provenance require explicit migration.
    connection.execute("INSERT INTO memory_embedding_space VALUES (1,?,NULL)", (json.dumps(profile, sort_keys=True),))
    return EmbeddingSpace(dict(profile), None)


def reset_empty_space(connection: sqlite3.Connection, profile: dict[str, str | None]) -> None:
    if connection.execute("SELECT 1 FROM memory_embeddings LIMIT 1").fetchone() or connection.execute(
            "SELECT 1 FROM memory_embedding_vec LIMIT 1").fetchone():
        raise RuntimeError("Discard source vectors and embedding metadata before changing embedding space")
    connection.execute("INSERT OR REPLACE INTO memory_embedding_space VALUES (1,?,NULL)", (json.dumps(profile, sort_keys=True),))


def require_vector(connection: sqlite3.Connection, profile: dict[str, str | None], dimension: int) -> None:
    recorded = read_space(connection)
    if recorded is None or not accepts_vector(recorded.profile == profile, recorded.dimension, dimension):
        raise RuntimeError("Vector identity or dimension differs from this generation; migrate embedding space first")
    if recorded.dimension is None:
        connection.execute("UPDATE memory_embedding_space SET dimension=? WHERE singleton=1", (dimension,))


def verify_space(connection: sqlite3.Connection) -> None:
    space = read_space(connection)
    metadata = connection.execute("SELECT DISTINCT provider_id,model_name,model_revision,text_processing_version FROM memory_embeddings").fetchall()
    dimensions = {row[0] for row in connection.execute("SELECT DISTINCT dimension FROM memory_embedding_vec")}
    if not metadata and not dimensions:
        return
    if space is None:
        raise RuntimeError("Existing embeddings have no declared identity; migrate embedding space first")
    expected = tuple(space.profile[key] for key in ("provider_id", "model_name", "model_revision", "text_processing_version"))
    if any(tuple(row) != expected for row in metadata) or (dimensions and dimensions != {space.dimension}):
        raise RuntimeError("Embedding metadata or vector dimensions mix different spaces")

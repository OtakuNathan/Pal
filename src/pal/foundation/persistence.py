from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Sequence

from peewee import DatabaseProxy, Model
from playhouse.sqlite_ext import SqliteDatabase


database_proxy: DatabaseProxy = DatabaseProxy()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BaseModel(Model):
    class Meta:
        database = database_proxy
        legacy_table_names = False


@dataclass
class RawSQLHookRegistry:
    statements: list[str] = field(default_factory=list)

    def register(self, statement: str) -> None:
        normalized = str(statement).strip()
        if normalized:
            self.statements.append(normalized)

    def iter_statements(self) -> tuple[str, ...]:
        return tuple(self.statements)


@dataclass
class RepositoryBase:
    def __init__(self, database: "PalV2Database") -> None:
        self._database = database


@dataclass
class PalV2Database:
    db_path: Path
    raw_sql_hooks: RawSQLHookRegistry = field(default_factory=RawSQLHookRegistry)

    def __post_init__(self) -> None:
        self._database = SqliteDatabase(
            self.db_path,
            pragmas={
                "foreign_keys": 1,
                "journal_mode": "wal",
            },
            check_same_thread=False,
        )

    @property
    def peewee_db(self) -> SqliteDatabase:
        return self._database

    def initialize(self, models: Sequence[type[Model]]) -> None:
        # PalV2 assumes schema preparation and migrations are handled outside
        # the runtime. Initialization only binds models and installs optional
        # raw SQL extensions needed by the already-prepared database.
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        database_proxy.initialize(self._database)
        self._database.connect(reuse_if_open=True)
        self._database.bind(models, bind_refs=True, bind_backrefs=True)
        self._database.create_tables(list(models), safe=True)
        self.install_raw_sql_extensions()

    def install_raw_sql_extensions(self) -> None:
        statements = self.raw_sql_hooks.iter_statements()
        if not statements:
            return
        with self._database.atomic():
            for statement in statements:
                self._database.execute_sql(statement)

    @contextmanager
    def transaction(self) -> Iterator[SqliteDatabase]:
        with self._database.atomic():
            yield self._database

    def close(self) -> None:
        if not self._database.is_closed():
            self._database.close()

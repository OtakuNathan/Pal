from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from pal.shared.text_search import compile_jieba_fts_queries


@dataclass
class MinionSearchStore:
    owner: Any

    def search_fts(
        self,
        db: sqlite3.Connection,
        *,
        table_name: str,
        id_column: str,
        query: str,
        limit: int,
        fallback_sql: str,
    ) -> list[dict[str, Any]]:
        normalized = str(query or "").strip()
        resolved_limit = max(1, min(int(limit or 10), 50))
        if not normalized:
            rows = [
                {"id": str(row[0]), "score": float(row[1])}
                for row in db.execute(fallback_sql, ("%%", resolved_limit)).fetchall()
            ]
            return _dedupe_scored_rows(rows, limit=resolved_limit)
        rows: list[dict[str, Any]] = []
        for fts_query, query_weight in compile_jieba_fts_queries(normalized):
            try:
                cursor = db.execute(
                    f"""
                    SELECT {id_column}, -bm25({table_name}) AS score
                    FROM {table_name}
                    WHERE {table_name} MATCH ?
                    ORDER BY bm25({table_name})
                    LIMIT ?
                    """,
                    (fts_query, resolved_limit),
                )
            except sqlite3.OperationalError:
                continue
            rows.extend({"id": str(row[0]), "score": float(row[1]) * float(query_weight)} for row in cursor.fetchall())
            if rows:
                break
        if not rows:
            like = f"%{normalized.lower()}%"
            rows.extend({"id": str(row[0]), "score": float(row[1])} for row in db.execute(fallback_sql, (like, resolved_limit)).fetchall())
        return _dedupe_scored_rows(
            sorted(rows, key=lambda item: (-float(item["score"]), item["id"])),
            limit=resolved_limit,
        )


def _dedupe_scored_rows(rows: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        row_id = str(row.get("id") or "")
        if not row_id or row_id in seen:
            continue
        seen.add(row_id)
        ordered.append(row)
        if len(ordered) >= limit:
            break
    return ordered
